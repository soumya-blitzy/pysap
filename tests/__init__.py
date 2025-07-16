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
import sys
import unittest


def main():
    """Execute comprehensive test suite with proper exit handling for Python 3.13+.
    
    This function provides Python 3-compatible test discovery and execution
    with proper exit code handling for CI systems and automated testing.
    """
    # Discover tests using Python 3.13+ compatible test loader
    loader = unittest.defaultTestLoader
    test_suite = loader.discover('.', pattern='*_test.py')
    
    # Configure test runner with Python 3.13+ features
    test_runner = unittest.TextTestRunner(
        verbosity=2, 
        resultclass=unittest.TextTestResult
    )
    
    # Execute test suite and capture results
    result = test_runner.run(test_suite)
    
    # Ensure CI systems receive proper exit codes
    sys.exit(not result.wasSuccessful())


# Export test_suite for programmatic access
test_suite = unittest.defaultTestLoader.discover('.', '*_test.py')


if __name__ == '__main__':
    main()
