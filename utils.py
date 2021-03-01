#!/usr/bin/env python3
# Description: Common helper functions

import hashlib
import pathlib
import shutil
import subprocess


def create_gitignore(folder):
    """
    Create a gitignore that ignores all files in a folder. Some folders are not
    known until the script is run so they can't be added to the root .gitignore
    :param folder: Folder to create the gitignore in
    """
    with folder.joinpath(".gitignore").open("w") as gitignore:
        gitignore.write("*")


def fetch_binutils(folder, update=True):
    """
    Clones/updates the binutils repo
    :param folder: Directory to download binutils to
    """
    binutils_folder = folder.joinpath("binutils")
    if binutils_folder.is_dir():
        if update:
            print_header("Updating binutils")
            subprocess.run(
                ["git", "-C",
                 binutils_folder.as_posix(), "pull", "--rebase"],
                check=True)
    else:
        print_header("Downloading binutils")
        subprocess.run([
            "git", "clone", "git://sourceware.org/git/binutils-gdb.git",
            binutils_folder.as_posix()
        ],
                       check=True)


def print_header(string):
    """
    Prints a fancy header
    :param string: String to print inside the header
    """
    # Use bold cyan for the header so that the headers
    # are not intepreted as success (green) or failed (red)
    print("\033[01;36m")
    for x in range(0, len(string) + 6):
        print("=", end="")
    print("\n== %s ==" % string)
    for x in range(0, len(string) + 6):
        print("=", end="")
    # \033[0m resets the color back to the user's default
    print("\n\033[0m")


def print_error(string):
    """
    Prints a error in bold red
    :param string: String to print
    """
    # Use bold red for error
    print("\033[01;31m%s\n\033[0m" % string)
