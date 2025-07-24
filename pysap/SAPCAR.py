# encoding: utf-8
# pysap - Python library for crafting SAP's network protocols packets
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# Author:
#   Martin Gallo (@martingalloar)
#   Code contributed by SecureAuth to the OWASP CBAS project
#

# Standard imports
import stat
from zlib import crc32
from struct import pack
from datetime import datetime, timezone
from os import stat as os_stat
from io import BytesIO
# External imports
from scapy.packet import Packet
from scapy.fields import (ByteField, ByteEnumField, LEIntField, FieldLenField,
                          PacketField, StrFixedLenField, PacketListField,
                          ConditionalField, LESignedIntField, StrField, LELongField)
# Custom imports
from pysap.utils.fields import (PacketNoPadded, StrNullFixedLenField, PacketListStopField)
from pysapcompress import (decompress, compress, ALG_LZH, CompressError,
                           DecompressError)

# Helper to ensure blocks is always a list, not None
def ensure_blocks_list(obj):
    # Ensure blocks is a list, not None, for both attribute and _fields dict
    if getattr(obj, 'blocks', None) is None:
        setattr(obj, 'blocks', [])
    if hasattr(obj, '_fields') and obj._fields.get('blocks', None) is None:
        obj._fields['blocks'] = []
    return obj.blocks


# Filemode code obtained from Python 3 stat.py
_filemode_table = (
    ((stat.S_IFLNK,         "l"),
     (stat.S_IFREG,         "-"),
     (stat.S_IFBLK,         "b"),
     (stat.S_IFDIR,         "d"),
     (stat.S_IFCHR,         "c"),
     (stat.S_IFIFO,         "p")),

    ((stat.S_IRUSR,         "r"),),
    ((stat.S_IWUSR,         "w"),),
    ((stat.S_IXUSR | stat.S_ISUID, "s"),
     (stat.S_ISUID,         "S"),
     (stat.S_IXUSR,         "x")),

    ((stat.S_IRGRP,         "r"),),
    ((stat.S_IWGRP,         "w"),),
    ((stat.S_IXGRP | stat.S_ISGID, "s"),
     (stat.S_ISGID,         "S"),
     (stat.S_IXGRP,         "x")),

    ((stat.S_IROTH,         "r"),),
    ((stat.S_IWOTH,         "w"),),
    ((stat.S_IXOTH | stat.S_ISVTX, "t"),
     (stat.S_ISVTX,         "T"),
     (stat.S_IXOTH,         "x"))
)


SIZE_FOUR_GB = 0xffffffff + 1


def filemode(mode):
    """Convert a file's mode to a string of the form '-rwxrwxrwx'."""
    perm = []
    for table in _filemode_table:
        for bit, char in table:
            if mode & bit == bit:
                perm.append(char)
                break
        else:
            perm.append("-")
    return "".join(perm)


class SAPCARInvalidFileException(Exception):
    """Exception to denote an invalid SAP CAR file"""


class SAPCARInvalidChecksumException(Exception):
    """Exception to denote a syntactically valid SAP CAR file with an invalid checksum"""


class SAPCARCompressedBlobFormat(PacketNoPadded):
    """SAP CAR compressed blob

    This is used for decompressing blobs inside the compressed block.
    """
    name = "SAP CAR Archive Compressed blob"

    fields_desc = [
        LEIntField("compressed_length", None),
        LEIntField("uncompress_length", None),
        ByteEnumField("algorithm", 0x12, {0x12: "LZH", 0x10: "LZC"}),
        StrFixedLenField("magic_bytes", b"\x1f\x9d", 2),
        ByteField("special", 2),
        ConditionalField(StrField("blob_small", None, remain=4), lambda x: x.compressed_length <= 8),
        ConditionalField(StrFixedLenField("blob_large", None, length_from=lambda x: x.compressed_length - 8),
                         lambda x: x.compressed_length > 8),
    ]


SAPCAR_BLOCK_TYPE_COMPRESSED_LAST = b"ED"
"""SAP CAR compressed end of data block"""

SAPCAR_BLOCK_TYPE_COMPRESSED = b"DA"
"""SAP CAR compressed block"""

SAPCAR_BLOCK_TYPE_UNCOMPRESSED_LAST = b"UE"
"""SAP CAR uncompressed end of data block"""

SAPCAR_BLOCK_TYPE_UNCOMPRESSED = b"UD"
"""SAP CAR uncompressed block"""


class SAPCARCompressedBlockFormat(PacketNoPadded):
    """SAP CAR compressed block

    This is used for decompressing blocks inside the file info format.
    """
    name = "SAP CAR Archive Compressed block"

    fields_desc = [
        StrFixedLenField("type", SAPCAR_BLOCK_TYPE_COMPRESSED_LAST, 2),
        ConditionalField(PacketField("compressed", None, SAPCARCompressedBlobFormat),
                         lambda x: x.type in [SAPCAR_BLOCK_TYPE_COMPRESSED_LAST, SAPCAR_BLOCK_TYPE_COMPRESSED]),
        ConditionalField(LESignedIntField("checksum", 0),
                         lambda x: x.type == SAPCAR_BLOCK_TYPE_COMPRESSED_LAST),
    ]


def sapcar_is_last_block(packet):
    """Helper function that evaluates if a block packet is the end of data one or not.

    :param packet: packet to check
    :type packet: Packet

    :return: if the block packet is the end of data one
    :rtype: bool
    """
    block_type = packet.type
    if isinstance(block_type, str):
        block_type = block_type.encode('utf-8')
    return block_type in [SAPCAR_BLOCK_TYPE_COMPRESSED_LAST, SAPCAR_BLOCK_TYPE_UNCOMPRESSED_LAST]


SAPCAR_TYPE_FILE = "RG"
"""SAP CAR regular file string"""

SAPCAR_TYPE_DIR = "DR"
"""SAP CAR directory string"""

SAPCAR_TYPE_SHORTCUT = "SC"
"""SAP CAR Windows short cut string"""

SAPCAR_TYPE_LINK = "LK"
"""SAP CAR Unix soft link string"""

SAPCAR_TYPE_AS400 = "SV"
"""SAP CAR AS400 save file string"""

SAPCAR_TYPE_SIGNATURE = "SM"
"""SAP CAR SIGNATURE.SMF file string"""
# XXX: Unsure if this file has any particular treatment in latest versions of SAPCAR

SAPCAR_VERSION_200 = b"2.00"
"""SAP CAR file format version 2.00 string"""

SAPCAR_VERSION_201 = b"2.01"
"""SAP CAR file format version 2.01 string"""


class SAPCARArchiveFilev200Format(PacketNoPadded):
    """SAP CAR file information format

    This is ued to parse files inside a SAP CAR archive.
    """
    name = "SAP CAR Archive File 2.00"

    version = SAPCAR_VERSION_200
    is_filename_null_terminated = False

    def __init__(self, *args, **kwargs):
        super(SAPCARArchiveFilev200Format, self).__init__(*args, **kwargs)
        # Ensure blocks is always a list
        if not hasattr(self, 'blocks') or self.blocks is None:
            self.blocks = []

    fields_desc = [
        StrFixedLenField("type", SAPCAR_TYPE_FILE, 2),
        LEIntField("perm_mode", 0),
        LELongField("file_length_low", 0),
        LEIntField("file_length_high", 0),
        LELongField("timestamp", 0),
        LEIntField("code_page", 0),
        FieldLenField("user_info_length", 0, length_of="user_info", fmt="<H"),
        FieldLenField("filename_length", 0, length_of="filename", fmt="<H"),
        StrNullFixedLenField("filename", None, length_from=lambda x: x.filename_length,
                             null_terminated=lambda x: x.is_filename_null_terminated),
        StrFixedLenField("user_info", None, length_from=lambda x: x.user_info_length),
        # blocks field moved to the end
    ]
    fields_desc.append(
        ConditionalField(PacketListStopField("blocks", None, SAPCARCompressedBlockFormat, stop=sapcar_is_last_block),
                         lambda x: ((x.type == SAPCAR_TYPE_FILE) or (x.type == SAPCAR_TYPE_FILE.encode('utf-8'))) and (getattr(x, 'file_length_low', 0) > 0 or getattr(x, 'file_length_high', 0) > 0))
    )

    @property
    def file_length(self):
        """Getter for the file length fields. It converts the two length fields (low and high) as provided in the
        archive file into a long long integer.
        """
        return (self.file_length_high * SIZE_FOUR_GB) + self.file_length_low

    @file_length.setter
    def file_length(self, file_length):
        """Setter for the file length fields. It splits the long long integer int on the two length fields (low and
        high) as required by the archive file.
        """
        self.file_length_low = file_length & 0xffffffff
        self.file_length_high = file_length >> 32

    def extract(self, fd):
        """Extracts the archive file and writes the extracted file to the provided file object. Returns the checksum
        obtained from the archive. If blocks are uncompressed, the file is directly extracted. If the blocks are
        compressed, each block is added to a buffer, skipping the length field, and decompression is performed after
        the block marked as end of data. Expected length and compression header is obtained from the first block and
        checksum from the end of data block.

        :param fd: file-like object to write the extracted file to
        :type fd: file

        :return: checksum
        :rtype: int

        :raise DecompressError: If there's a decompression error
        :raise SAPCARInvalidFileException: If the file is invalid
        """

        if self.file_length == 0:
            return 0

        compressed = b""
        checksum = 0
        exp_length = None

        remaining_length = self.file_length
        for block in getattr(self, 'blocks', None) or []:
            # Ensure block.type is bytes for comparison
            block_type = block.type
            if isinstance(block_type, str):
                block_type = block_type.encode('utf-8')
            # Process uncompressed block types
            if block_type in [SAPCAR_BLOCK_TYPE_UNCOMPRESSED, SAPCAR_BLOCK_TYPE_UNCOMPRESSED_LAST]:
                fd.write(block.compressed)
                remaining_length -= len(block.compressed)
            # Store compressed block types for later decompression
            elif block_type in [SAPCAR_BLOCK_TYPE_COMPRESSED, SAPCAR_BLOCK_TYPE_COMPRESSED_LAST]:
                compressed += bytes(block.compressed)[4:]
                if not exp_length:
                    exp_length = block.compressed.uncompress_length
            else:
                raise SAPCARInvalidFileException("Invalid block type found")

            # Check end of data block, performing decompression if needed
            if sapcar_is_last_block(block):
                checksum = block.checksum
                # If there was at least one compressed block that set the expected length, decompress it
                if exp_length:
                    (_, block_length, block_buffer) = decompress(compressed, exp_length)
                    if block_length != exp_length or not block_buffer:
                        raise DecompressError("Error decompressing block")
                    fd.write(block_buffer)
                break

        return checksum


class SAPCARArchiveFilev201Format(SAPCARArchiveFilev200Format):
    """SAP CAR file information format

    This is used to parse files inside a SAP CAR archive.
    """
    name = "SAP CAR Archive File 2.01"

    version = SAPCAR_VERSION_201
    is_filename_null_terminated = True

    def __init__(self, *args, **kwargs):
        super(SAPCARArchiveFilev201Format, self).__init__(*args, **kwargs)
        # Ensure blocks is always a list
        if not hasattr(self, 'blocks') or self.blocks is None:
            self.blocks = []

    def extract(self, fd):
        """Extracts the archive file and writes the extracted file to the provided file object. Returns the checksum
        obtained from the archive. If blocks are uncompressed, the file is directly extracted. If the blocks are
        compressed, each block is added to a buffer, skipping the length field, and decompression is performed after
        the block marked as end of data. Expected length and compression header is obtained from the first block and
        checksum from the end of data block.

        :param fd: file-like object to write the extracted file to
        :type fd: file

        :return: checksum
        :rtype: int

        :raise DecompressError: If there's a decompression error
        :raise SAPCARInvalidFileException: If the file is invalid
        """
        return super().extract(fd)


SAPCAR_HEADER_MAGIC_STRING_STANDARD = b"CAR\x20"
"""SAP CAR archive header magic string standard"""

SAPCAR_HEADER_MAGIC_STRING_BACKUP = b"CAR\x00"
"""SAP CAR archive header magic string backup file"""


sapcar_archive_file_versions = {
    SAPCAR_VERSION_200: SAPCARArchiveFilev200Format,
    SAPCAR_VERSION_201: SAPCARArchiveFilev201Format,
}
"""SAP CAR file format versions"""


class SAPCARArchiveFormat(Packet):
    """SAP CAR file format

    This is used to parse SAP CAR archive files.
    """
    name = "SAP CAR Archive"

    fields_desc = [
        StrFixedLenField("magic_string", SAPCAR_HEADER_MAGIC_STRING_STANDARD, 4),
        StrFixedLenField("version", SAPCAR_VERSION_201, 4),
        ConditionalField(PacketListField("files0", None, SAPCARArchiveFilev200Format),
                         lambda x: x.version == SAPCAR_VERSION_200),
        ConditionalField(PacketListField("files1", None, SAPCARArchiveFilev201Format),
                         lambda x: x.version == SAPCAR_VERSION_201),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if hasattr(self, 'files0') and self.files0 is not None:
            pass
        if hasattr(self, 'files1') and self.files1 is not None:
            pass


class SAPCARArchiveFile(object):
    """Proxy class that can be used to access a file inside a SAP CAR
    archive and obtain its properties.
    """

    # Instance attributes
    _file_format = None

    def __init__(self, file_format=None):
        """Construct the file proxy object from a L{SAPCARArchiveFilev200Format}
        or L{SAPCARArchiveFilev201Format} object.

        :param file_format: file format object
        :type file_format: Packet
        """
        self._file_format = file_format

    def is_file(self):
        t = self._file_format.type
        return t == SAPCAR_TYPE_FILE or t == SAPCAR_TYPE_FILE.encode('utf-8')

    def is_directory(self):
        t = self._file_format.type
        return t == SAPCAR_TYPE_DIR or t == SAPCAR_TYPE_DIR.encode('utf-8')

    @property
    def version(self):
        """The version of the file.

        :return: version of the file
        :rtype: string
        """
        v = self._file_format.version
        if isinstance(v, bytes):
            return v.decode('utf-8')
        return v

    @version.setter
    def version(self, version):
        """Sets the version of the file. If the version is different to the current one, it
        converts the archive file.

        :param version: version to set
        :type version: string
        """
        if isinstance(version, str):
            version = version.encode('utf-8')
        self._file_format.version = version

    @property
    def filename(self):
        f = self._file_format.filename
        if isinstance(f, bytes):
            # Strip trailing nulls before decoding
            f = f.rstrip(b'\x00')
            try:
                return f.decode('utf-8').rstrip('\x00')
            except UnicodeDecodeError:
                return f.decode('latin1').rstrip('\x00')
        elif isinstance(f, str):
            return f.rstrip('\x00')
        return f

    @filename.setter
    def filename(self, filename):
        if isinstance(filename, bytes):
            filename = filename.decode('utf-8', errors='replace')
        # For v2.01, ensure null-terminated and length includes null
        if self.version == '2.01':
            if not filename.endswith('\x00'):
                filename_bytes = (filename + '\x00').encode('utf-8')
            else:
                filename_bytes = filename.encode('utf-8')
            self._file_format.filename = filename_bytes
            self._file_format.filename_length = len(filename_bytes)
        else:
            filename_bytes = filename.encode('utf-8')
            self._file_format.filename = filename_bytes
            self._file_format.filename_length = len(filename_bytes)

    @property
    def size(self):
        """The size of the file.

        :return: size of the file
        :rtype: int
        """
        return self._file_format.file_length

    @size.setter
    def size(self, file_length):
        """Sets the size of the file.

        :param file_length: the size of the file
        :type file_length: int
        """
        self._file_format.file_length = file_length

    @property
    def permissions(self):
        """The permissions of the file.

        :return: permissions in human-readable format
        :rtype: string
        """
        return filemode(self._file_format.perm_mode)

    @permissions.setter
    def permissions(self, perm_mode):
        """Sets the permissions on the file.

        :param perm_mode: the permissions to set
        :type perm_mode: int
        """
        self._file_format.perm_mode = perm_mode

    @property
    def perm_mode(self):
        """The permissions mode of the file.

        :return: permissions in numeric format
        :rtype: int
        """
        return self._file_format.perm_mode

    @property
    def timestamp(self):
        """The timestamp of the file.

        :return: timestamp in human-readable format
        :rtype: string
        """
        return datetime.fromtimestamp(self._file_format.timestamp, timezone.utc).strftime('%d %b %Y %H:%M')

    @timestamp.setter
    def timestamp(self, timestamp):
        """Sets the file timestamp.

        :param timestamp: the timestamp to set
        :type timestamp: int
        """
        self._file_format.timestamp = timestamp

    @property
    def timestamp_raw(self):
        """The timestamp of the file.

        :return: timestamp in numeric format
        :rtype: int
        """
        return self._file_format.timestamp

    @property
    def checksum(self):
        """The checksum of the file.

        :return: checksum
        :rtype: int

        :raise SAPCARInvalidFileException: if the file is invalid and contains more than one end of data block
        """
        checksum = None
        for block in getattr(self._file_format, 'blocks', None) or []:
            if block.type == SAPCAR_BLOCK_TYPE_COMPRESSED_LAST:
                if checksum is not None:
                    raise SAPCARInvalidFileException("More than one end of data block found for the file")
                checksum = block.checksum
        return checksum

    @checksum.setter
    def checksum(self, checksum):
        """Sets the file checksum.

        :param checksum: checksum to set
        :rtype checksum: int

        :raise SAPCARInvalidFileException: if the file is invalid and contains more than one end of data block
        """
        checksum_set = False
        for block in getattr(self._file_format, 'blocks', None) or []:
            if block.type == SAPCAR_BLOCK_TYPE_COMPRESSED_LAST:
                if checksum_set:
                    raise SAPCARInvalidFileException("More than one end of data block found for the file")
                block.checksum = checksum
                checksum_set = True
        if not checksum_set:
            raise SAPCARInvalidFileException("No end of data block found for the file")

    @staticmethod
    def calculate_checksum(data):
        """Calculates the CRC32 checksum of a given data string.

        :param data: data to calculate the checksum over
        :type data: str

        :return: the CRC32 checksum
        :rtype: int
        """
        return -crc32(data, -1) - 1

    @classmethod
    def from_file(cls, filename, version=SAPCAR_VERSION_201, archive_filename=None):
        """Populates the file format object from an actual file on the
        local file system.

        :param filename: filename to build the file format object from
        :type filename: string

        :param version: version of the file to construct
        :type version: string

        :param archive_filename: filename to use inside the archive file
        :type archive_filename: string

        :raise ValueError: if the version requested is invalid
        """

        # Read the file properties and its content
        stat = os_stat(filename)
        with open(filename, "rb") as fd:
            data = fd.read()

        # Compress the file content and build the compressed string
        try:
            (_, out_length, out_buffer) = compress(data, ALG_LZH)
        except CompressError:
            return None
        out_buffer = pack("<I", out_length) + out_buffer

        # Check the version and grab the file format class
        # Ensure version is bytes for field assignment, but str for API
        if isinstance(version, str):
            version_bytes = version.encode('utf-8')
        else:
            version_bytes = version
        if version_bytes not in sapcar_archive_file_versions:
            raise ValueError("Invalid version")
        ff = sapcar_archive_file_versions[version_bytes]

        # If an archive filename was not provided, use the actual filename
        if archive_filename is None:
            archive_filename = filename
        # Ensure archive_filename is str
        if isinstance(archive_filename, bytes):
            archive_filename = archive_filename.decode('utf-8', errors='replace')
        # For v2.01, ensure null-terminated and length includes null
        if (isinstance(version, str) and version == '2.01') or (isinstance(version, bytes) and version == b'2.01'):
            if not archive_filename.endswith('\x00'):
                archive_filename_bytes = (archive_filename + '\x00').encode('utf-8')
            else:
                archive_filename_bytes = archive_filename.encode('utf-8')
            filename_length = len(archive_filename_bytes)
        else:
            archive_filename_bytes = archive_filename.encode('utf-8')
            filename_length = len(archive_filename_bytes)

        # Build the object and fill the fields
        archive_file = cls()
        archive_file._file_format = ff()
        archive_file._file_format.perm_mode = stat.st_mode
        archive_file._file_format.timestamp = int(stat.st_atime)
        archive_file._file_format.file_length = stat.st_size
        archive_file._file_format.filename = archive_filename_bytes
        archive_file._file_format.filename_length = filename_length
        # Put the compressed blob inside a end of data block and add it to the object
        block = SAPCARCompressedBlockFormat()
        block.type = SAPCAR_BLOCK_TYPE_COMPRESSED_LAST
        block.compressed = SAPCARCompressedBlobFormat(out_buffer)
        block.checksum = cls.calculate_checksum(data)
        if 'blocks' not in archive_file._file_format.__dict__ or not isinstance(archive_file._file_format.__dict__['blocks'], list):
            archive_file._file_format.__dict__['blocks'] = []
        archive_file._file_format.blocks.append(block)

        return archive_file

    @classmethod
    def from_archive_file(cls, archive_file, version=SAPCAR_VERSION_201):
        """Populates the file format object from another archive file object.

        :param archive_file: archive file object to build the file format object from
        :type archive_file: L{SAPCARArchiveFile}

        :param version: version of the file to construct
        :type version: string

        :raise ValueError: if the version requested is invalid
        """

        if version not in sapcar_archive_file_versions:
            raise ValueError("Invalid version")
        ff = sapcar_archive_file_versions[version]

        new_archive_file = cls()
        new_archive_file._file_format = ff()
        new_archive_file._file_format.type = archive_file._file_format.type
        new_archive_file._file_format.perm_mode = archive_file._file_format.perm_mode
        new_archive_file._file_format.timestamp = int(archive_file._file_format.timestamp)
        new_archive_file._file_format.file_length = archive_file._file_format.file_length
        new_archive_file._file_format.filename = archive_file._file_format.filename
        new_archive_file._file_format.filename_length = archive_file._file_format.filename_length
        blocks_to_copy = list(getattr(archive_file._file_format, 'blocks', None) or [])
        if not blocks_to_copy:
            new_archive_file._file_format.blocks = []
        for block in blocks_to_copy:
            new_block = SAPCARCompressedBlockFormat()
            new_block.type = block.type
            new_block.compressed = SAPCARCompressedBlobFormat(bytes(block.compressed))
            new_block.checksum = block.checksum
            if not hasattr(new_archive_file._file_format, 'blocks') or new_archive_file._file_format.blocks is None:
                new_archive_file._file_format.blocks = []
            new_archive_file._file_format.blocks.append(new_block)

        return new_archive_file

    def open(self, enforce_checksum=False):
        """Opens the compressed file and returns a file-like object that
        can be used to access its uncompressed content.

        :param enforce_checksum: If the checksum validation should be enforce
        :type enforce_checksum: bool

        :return: file-like object with the uncompressed file content
        :rtype: file

        :raise Exception: If the file to open is a directory
        :raise DecompressError: If there's a decompression error
        :raise SAPCARInvalidFileException: If the file is invalid
        :raise SAPCARInvalidChecksumException: If the checksum is invalid
        """
        # Check that the type is file, so we don't try to extract from a directory
        if self.is_directory():
            raise Exception("Invalid file type")

        # Extract the file to a file-like object
        out_file = BytesIO()
        checksum = self._file_format.extract(out_file)
        out_file.seek(0)

        # Validate the checksum if required
        if enforce_checksum:
            if checksum != self.calculate_checksum(out_file.getvalue()):
                raise SAPCARInvalidChecksumException("Invalid checksum found")
            out_file.seek(0)

        # Return the extracted file
        return out_file

    def check_checksum(self):
        """Checks if the checksum of the file is valid.

        :return: if the checksum matches
        :rtype: bool
        """
        if self.size == 0:
            return True
        crc = self.calculate_checksum(self.open().read())
        return crc == self.checksum


class SAPCARArchive(object):
    """Proxy class that can be used to read SAP CAR archive files.
    """

    # Instance attributes
    filename = None
    fd = None
    _sapcar = None

    def __init__(self, fil, mode="rb+", version=SAPCAR_VERSION_201):
        """Opens an archive file and allow access to it.

        :param fil: filename or file descriptor to open
        :type fil: string or file

        :param mode: mode to open the file
        :type mode: string

        :param version: archive file version to use when creating
        :type version: string
        """

        # Ensure version is withing supported versions
        if version not in sapcar_archive_file_versions:
            raise ValueError("Invalid version")

        # Ensure mode is within supported modes
        if mode not in ["r", "r+", "w", "w+", "rb", "rb+", "wb", "wb+"]:
            raise ValueError("Invalid mode")

        # Ensure file is open in binary mode
        if "b" not in mode:
            mode += "b"

        if isinstance(fil, str):
            self.filename = fil
            self.fd = open(fil, mode)
        else:
            self.filename = getattr(fil, "name", None)
            self.fd = fil

        if "r" in mode:
            self.read()
        else:
            self.create()
            self.version = version

    @property
    def files(self):
        import string
        fils = {}
        if self._files:
            for idx, fil in enumerate(self._files):
                # Only include regular files (handle bytes/str)
                file_type = getattr(fil, 'type', None)
                if isinstance(file_type, bytes):
                    file_type_decoded = file_type.decode('utf-8', errors='replace')
                else:
                    file_type_decoded = file_type
                if file_type_decoded != SAPCAR_TYPE_FILE:
                    continue
                # Defensive fix: ensure blocks is always a list
                if hasattr(fil, 'blocks') and fil.blocks is None:
                    fil.blocks = []
                raw_fname = fil.filename
                fname = raw_fname
                if isinstance(fname, bytes):
                    fname = fname.rstrip(b'\x00')
                    try:
                        fname = fname.decode('utf-8').rstrip('\x00')
                    except UnicodeDecodeError:
                        fname = fname.decode('latin1').rstrip('\x00')
                elif isinstance(fname, str):
                    fname = fname.rstrip('\x00')
                is_non_empty = bool(fname)
                is_printable = all(c in string.printable for c in fname)
                is_not_ws = not fname.isspace()
                if not is_non_empty:
                    continue
                if not is_printable:
                    continue
                if not is_not_ws:
                    continue
                if is_non_empty and is_printable and is_not_ws:
                    fils[fname] = SAPCARArchiveFile(fil)
        return fils

    @property
    def files_names(self):
        """The list of file names inside this archive file.

        :return: list of file names
        :rtype: L{list} of L{string}
        """
        return [name.rstrip('\x00') if isinstance(name, str) else name.rstrip(b'\x00').decode('utf-8').rstrip('\x00') for name in self.files.keys()]

    @property
    def version(self):
        v = self._sapcar.version
        if isinstance(v, bytes):
            return v.decode('utf-8')
        return v

    @version.setter
    def version(self, version):
        if isinstance(version, str):
            version_bytes = version.encode('utf-8')
        else:
            version_bytes = version
        # Only convert if version is actually changing
        current_version = self._sapcar.version
        if isinstance(current_version, bytes):
            current_version_str = current_version.decode('utf-8')
        else:
            current_version_str = current_version
        if isinstance(version, bytes):
            new_version_str = version.decode('utf-8')
        else:
            new_version_str = version
        if current_version_str != new_version_str:
            # Convert all file objects to new version
            old_files = self._files or []
            from_file_version = sapcar_archive_file_versions.get(current_version if isinstance(current_version, bytes) else current_version.encode('utf-8'))
            to_file_version = sapcar_archive_file_versions.get(version_bytes)
            if to_file_version is not None:
                new_files = []
                for fil in old_files:
                    proxy = SAPCARArchiveFile(fil)
                    new_file = SAPCARArchiveFile.from_archive_file(proxy, version=version_bytes)
                    new_files.append(new_file._file_format)
                if new_version_str == '2.00':
                    self._sapcar.files0 = new_files
                    self._sapcar.files1 = None
                else:
                    self._sapcar.files1 = new_files
                    self._sapcar.files0 = None
        self._sapcar.version = version_bytes

    def read(self):
        """Reads the SAP CAR archive file and populates the files list.

        :raise Exception: if the file is invalid or unsupported
        """
        self.fd.seek(0)
        self._sapcar = SAPCARArchiveFormat(self.fd.read())
        # Post-process: ensure all file objects have blocks as a list
        if hasattr(self._sapcar, 'files0') and self._sapcar.files0 is not None:
            for fil in self._sapcar.files0:
                # Defensive: ensure blocks is always a list
                if hasattr(fil, 'blocks') and fil.blocks is None:
                    fil.blocks = []
        if hasattr(self._sapcar, 'files1') and self._sapcar.files1 is not None:
            for fil in self._sapcar.files1:
                # Defensive: ensure blocks is always a list
                if hasattr(fil, 'blocks') and fil.blocks is None:
                    fil.blocks = []
        if self._sapcar.magic_string not in [SAPCAR_HEADER_MAGIC_STRING_STANDARD, SAPCAR_HEADER_MAGIC_STRING_BACKUP]:
            raise Exception("Invalid or unsupported magic string in file")
        if self._sapcar.version not in sapcar_archive_file_versions:
            raise Exception("Invalid or unsupported version in file")

    @property
    def _files(self):
        """The file format objects according to the version.

        :return: files format objects according to the version
        """
        # Compare as string for version
        if str(self.version) == '2.00':
            files0 = self._sapcar.files0
            return files0 if files0 is not None else []
        else:
            files1 = self._sapcar.files1
            return files1 if files1 is not None else []

    @_files.setter
    def _files(self, files):
        if self.version == SAPCAR_VERSION_200:
            self._sapcar.files0 = files
        else:
            self._sapcar.files1 = files

    def create(self):
        """Creates the structure for holding a new SAP CAR archive file.
        """
        self._sapcar = SAPCARArchiveFormat()

    def write(self):
        """Writes the SAP CAR archive file to the file descriptor.
        """
        self.fd.seek(0)
        self.fd.write(bytes(self._sapcar))
        self.fd.flush()

    def write_as(self, filename=None):
        """Writes the SAP CAR archive file to another file.

        :param filename: name of the file to write to
        :type filename: string
        """
        if not filename:
            self.write()
        else:
            with open(filename, "wb") as fd:
                fd.write(bytes(self._sapcar))

    def add_file(self, filename, archive_filename=None):
        """Adds a new file to the SAP CAR archive file.

        :param filename: name of the file to add
        :type filename: string

        :param archive_filename: name of the file to use in the archive
        :type archive_filename: string
        """
        fil = SAPCARArchiveFile.from_file(filename, self.version, archive_filename)
        if self._files is None:
            self._files = []
        self._files.append(fil._file_format)

    def open(self, filename):
        """Returns a file-like object that can be used to access a file
        inside the SAP CAR archive.

        :param filename: name of the file to open
        :type filename: string

        :return: a file-like object that can be used to access the decompressed file.
        :rtype: file
        """
        if filename not in self.files:
            raise Exception("Invalid filename")
        return self.files[filename].open()

    def close(self):
        """Close the file descriptor object associated to the archive file.
        """
        self.fd.close()

    def raw(self):
        """Returns the raw data of the archive file.

        :return: raw data
        :rtype: bytes
        """
        if self._sapcar:
            return bytes(self._sapcar)
        return b""
