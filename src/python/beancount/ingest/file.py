"""Filing script.

Read an import script and a list of downloaded filenames or directories of
downloaded files, and for each of those files, move the file under an account
corresponding to the filing directory.
"""
__author__ = "Martin Blais <blais@furius.ca>"

from os import path
import datetime
import logging
import os
import shutil
import sys
import re

from beancount.core import account
from beancount.utils import misc_utils
from beancount.ingest import identify
from beancount.ingest import scripts_utils
from beancount.ingest import cache


def file(importer_config,
         files_or_directories,
         destination,
         dry_run=False,
         mkdirs=False,
         overwrite=False,
         idify=False,
         logfile=None):
    """File importable files under a destination directory.

    Given an importer configuration object, search for files that can be
    imported under the given list of files or directories and moved them under
    the given destination directory with the date computed by the module
    prepended to the filename. If the date cannot be extracted, use a reasonable
    default for the date (e.g. the last modified time of the file itself).

    If 'mkdirs' is True, create the destination directories before moving the
    files.

    Args:
      importer_config: a list of importer instances that define the config.
      files_or_directories: a list of files of directories to walk recursively and
        hunt for files to import.
      destination: A string, the root destination directory where the files are
        to be filed. The files are organized there under a hierarchy mirrorring
        that of the chart of accounts.
      dry_run: A flag, if true, don't actually move the files.
      mkdirs: A flag, if true, make all the intervening directories; otherwise,
        fail to move files to non-existing dirs.
      overwrite: A flag, if true, overwrite an existing destination file.
      idify: A flag, if true, remove whitespace and funky characters in the destination
        filename.
      logfile: A file object to write log entries to, or None, in which case no log is
        written out.
    """
    jobs = []
    has_errors = False
    for filename, importers in identify.find_imports(importer_config,
                                                     files_or_directories,
                                                     logfile):
        # If we're debugging, print out the match text.
        # This option is useful when we're building our importer configuration,
        # to figure out which patterns to create as unique signatures.
        if not importers:
            continue

        # Create an object to cache all the conversions between the importers
        # and phases and what-not.
        file = cache.FileMemo(filename)

        # Get the account corresponding to the file.
        file_accounts = set(importer.file_account(file)
                            for importer in importers)
        file_accounts.discard(None)
        if not file_accounts:
            logging.error("No account provided by importers: {}".format(
                ", ".join(imp.name() for imp in importers)))
            continue

        if len(file_accounts) > 1:
            logging.error("Ambiguous accounts from many importers: {}".format(
                ', '.join(file_accounts)))
            continue


        file_account = file_accounts.pop()

        # Given multiple importers, select the first one that was yielded to
        # obtain the date and process the filename.
        importer = importers[0]

        # Compute the date from the last modified time.
        mtime = path.getmtime(filename)
        mtime_date = datetime.datetime.fromtimestamp(mtime).date()

        # Try to get the file's date by calling a module support function. The
        # module may be able to extract the date from the filename, from the
        # contents of the file itself (e.g. scraping some text from the PDF
        # contents, or grabbing the last line of a CSV file).
        date = importer.file_date(file)
        date_source = 'mtime'
        if date is None:
            # Fallback on the last modified time of the file.
            date = mtime_date
            date_source = 'extracted'

        # Apply filename renaming, if implemented.
        # Otherwise clean up the filename.
        clean_filename = importer.file_name(file)
        if clean_filename is None:
            clean_filename = file.name
        elif re.match('\d\d\d\d-\d\d-\d\d', clean_filename):
            logging.error("The importer's file_name() method should not date the "
                          "returned filename.")

        # We need a simple filename; remove the directory part if there is one.
        clean_basename = path.basename(clean_filename)

        # Remove whitespace if requested.
        if idify:
            clean_basename = misc_utils.idify(clean_basename)

        # Prepend the date prefix.
        new_filename = '{0:%Y-%m-%d}.{1}'.format(date, clean_basename)

        # Prepend destination directory.
        new_fullname = path.normpath(path.join(destination,
                                               file_account.replace(account.sep, os.sep),
                                               new_filename))

        # Print the filename and which modules matched.
        if logfile is not None:
            logfile.write('=== {}\n'.format(filename))
            if importers:
                logfile.write('\n')
            logfile.write('  Importer:    {}\n'.format(importer.name() if importer else '-'))
            logfile.write('  Account:     {}\n'.format(file_account))
            logfile.write('  Date:        {} (from {})\n'.format(mtime_date, date_source))
            logfile.write('  Destination: {}\n'.format(new_fullname))
            logfile.write('\n')

        # Check if the destination directory exists.
        new_dirname = path.dirname(new_fullname)
        if not path.exists(new_dirname) and not mkdirs:
            logging.error("Destination directory '{}' does not exist.".format(new_dirname))
            has_errors = True
            continue

        # Check if the destination file already exists; we don't want to clobber
        # it by accident.
        if not overwrite and path.exists(new_fullname):
            logging.error("Destination file '{}' already exists.".format(new_fullname))
            has_errors = True
            continue

        jobs.append( (filename, new_fullname) )

    # If there are any errors, just don't do anything at all. This is a nicer
    # behaviour than moving just *some* files.
    if dry_run or has_errors:
        return

    # Actually carry out the moving job.
    for old_filename, new_filename in jobs:
        move_xdev_file(old_filename, new_filename, mkdirs)

    return jobs


def move_xdev_file(src_filename, dst_filename, mkdirs=False):
    """Move a file, potentially across devices.

    Args:
      src_filename: A string, the name of the file to copy.
      dst_filename: A string, where to copy the file.
      mkdirs: A flag, true if we should create a non-existing destination directory.
    """
    # Create missing directory if required.
    dst_dirname = path.dirname(dst_filename)
    if mkdirs:
        if not path.exists(dst_dirname):
            os.makedirs(dst_dirname)
    else:
        if not path.exists(dst_dirname):
            raise OSError("Destination directory '{}' does not exist.".format(dst_dirname))

    # Copy the file to its new name.
    shutil.copyfile(src_filename, dst_filename)

    # Remove the old file. Note that we copy and remove to support
    # cross-device moves, because it's sensible that the destination might
    # be on an encrypted device.
    os.remove(src_filename)


def main():
    parser = scripts_utils.create_arguments_parser(
        "Move and rename downloaded files to a documents tree "
        "mirrorring the chart of accounts")

    parser.add_argument('-o', '--output', '--output-dir', '--destination',
                        dest='output_dir', action='store', required=True,
                        help="The root of the documents tree to move the files to.")

    parser.add_argument('-n', '--dry-run', action='store_true',
                        help=("Just print where the files would be moved; "
                              "don't actually move them."))

    parser.add_argument('--no-overwrite', dest='overwrite',
                        action='store_false', default=True,
                        help="Don't overwrite destination files with the same name.")

    args, config, downloads_directories = scripts_utils.parse_arguments(parser)

    # Make sure the output directory exists.
    assert args.output_dir, "Internal error."
    if not path.exists(args.output_dir):
        parser.error("Output directory does not exist.")

    file(config, downloads_directories, args.output_dir,
         dry_run=args.dry_run,
         mkdirs=True,
         overwrite=args.overwrite,
         idify=True,
         logfile=sys.stdout)