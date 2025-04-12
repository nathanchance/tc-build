#!/usr/bin/env python3

import contextlib
from pathlib import Path
import subprocess
import textwrap
import time

from tc_build.builder import Builder
import tc_build.utils


def toml_boolean(boolean):
    if boolean:
        return 'true'
    return 'false'


class RustBuilder(Builder):

    def __init__(self):
        super().__init__()

        self.llvm_install_folder = None
        self.debug = False
        self.vendor_string = ""

    def build(self):
        if not self.folders.build:
            raise RuntimeError('No build folder set for build()?')
        if not Path(self.folders.source, 'bootstrap.toml').exists():
            raise RuntimeError('No bootstrap.toml in source folder, run configure()?')

        build_start = time.time()
        base_x_cmd = ['./x.py']
        # 'install' is used for simplicity.
        self.run_cmd([*base_x_cmd, 'install'], cwd=self.folders.source)

        tc_build.utils.print_info(f"Build duration: {tc_build.utils.get_duration(build_start)}")

        if self.folders.install:
            tc_build.utils.create_gitignore(self.folders.install)

    def configure(self):
        if not self.llvm_install_folder:
            raise RuntimeError('No LLVM install folder set?')
        if not self.folders.source:
            raise RuntimeError('No source folder set?')
        if not self.folders.build:
            raise RuntimeError('No build folder set?')

        # Generate the build configuration
        #
        # 'codegen-tests' requires '-DLLVM_INSTALL_UTILS=ON'.
        install_folder = self.folders.install if self.folders.install else self.folders.build
        with Path(self.folders.source, 'bootstrap.toml').open('w', encoding='utf-8') as file:
            file.write(
                textwrap.dedent(f'''\
                    change-id = "ignore"

                    [llvm]
                    download-ci-llvm = false

                    [build]
                    description = "{self.vendor_string}"
                    build-dir = "{self.folders.build}"
                    docs = false
                    locked-deps = true
                    extended = true
                    tools = [
                        "cargo",
                        "clippy",
                        "rustdoc",
                        "rustfmt",
                        "src",
                    ]
                    optimized-compiler-builtins = true

                    [install]
                    prefix = "{install_folder}"
                    sysconfdir = "etc"

                    [rust]
                    debug = {toml_boolean(self.debug)}
                    codegen-tests = false

                    [target.x86_64-unknown-linux-gnu]
                    llvm-config = "{self.llvm_install_folder}/bin/llvm-config"
                '''))

        self.clean_build_folder()

    def show_install_info(self):
        # Installation folder is optional, show build folder as the
        # installation location in that case.
        install_folder = self.folders.install if self.folders.install else self.folders.build
        if not install_folder:
            raise RuntimeError('Installation folder not set?')
        if not install_folder.exists():
            raise RuntimeError('Installation folder does not exist, run build()?')
        if not (bin_folder := Path(install_folder, 'bin')).exists():
            raise RuntimeError('bin folder does not exist in installation folder, run build()?')

        tc_build.utils.print_header('Rust installation information')
        install_info = (f"Toolchain is available at: {install_folder}\n\n"
                        'To use, either run:\n\n'
                        f"\t$ export PATH={bin_folder}:$PATH\n\n"
                        'or add:\n\n'
                        f"\tPATH={bin_folder}:$PATH\n\n"
                        'before the command you want to use this toolchain.\n')
        print(install_info)

        for tool in ['rustc', 'rustdoc', 'rustfmt', 'clippy-driver', 'cargo']:
            if (binary := Path(bin_folder, tool)).exists():
                subprocess.run([binary, '--version', '--verbose'], check=True)
                print()
        tc_build.utils.flush_std_err_out()


class RustSourceManager:

    def __init__(self, repo):
        self.repo = repo

    def download(self, ref):
        if self.repo.exists():
            return

        tc_build.utils.print_header('Downloading Rust')

        git_clone = ['git', 'clone', 'https://github.com/rust-lang/rust.git', self.repo]

        subprocess.run(git_clone, check=True)

        self.git(['checkout', ref])

    def git(self, cmd, capture_output=False):
        return subprocess.run(['git', *cmd],
                              capture_output=capture_output,
                              check=True,
                              cwd=self.repo,
                              text=True)

    def git_capture(self, cmd):
        return self.git(cmd, capture_output=True).stdout.strip()

    def ref_exists(self, ref):
        try:
            self.git(['show-branch', ref])
        except subprocess.CalledProcessError:
            return False
        return True

    def update(self, ref):
        tc_build.utils.print_header('Updating Rust')

        self.git(['fetch', 'origin'])

        self.git(['checkout', ref])

        local_ref = None
        with contextlib.suppress(subprocess.CalledProcessError):
            local_ref = self.git_capture(['symbolic-ref', '-q', 'HEAD'])
        if local_ref and local_ref.startswith('refs/heads/'):
            self.git(['pull', '--rebase', 'origin', local_ref.replace('refs/heads/', '')])
