#!/usr/bin/env python3

from argparse import ArgumentParser
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys

import requests

BENCHMARK = Path(__file__).resolve().parent
(RESULTS := Path(BENCHMARK, 'results')).mkdir(exist_ok=True)
REPO_ROOT = BENCHMARK.parent
BUILD_LLVM_PY = Path(REPO_ROOT, 'build-llvm.py')

# We have some nice common infrastructure for doing kernel builds, so import
# the tc_build package from the root of the repo.
sys.path.append(str(REPO_ROOT))
# pylint: disable=wrong-import-position
import tc_build.kernel  # noqa: E402
import tc_build.utils  # noqa: E402
# pylint: enable=wrong-import-position

# Create arguments
parser = ArgumentParser(
    description=
    'Benchmark different LLVM build time optimization technologies for building the Linux kernel faster'
)
parser.add_argument('-b',
                    '--build-folder',
                    default=Path(BENCHMARK, 'build'),
                    help='Location to build artifacts (default: %(default)s).',
                    type=Path)
parser.add_argument('-d',
                    '--dry-run',
                    action='store_true',
                    help='Do not run hyperfine or other build commands.')
parser.add_argument('-i',
                    '--install-folder',
                    default=Path(BENCHMARK, 'toolchains'),
                    help='Location to install toolchains (default: %(default)s).',
                    type=Path)
parser.add_argument('-k',
                    '--kernel-folder',
                    help='The path to the Linux kernel source code to build from (must be clean).',
                    required=True,
                    type=Path)
parser.add_argument('-l',
                    '--llvm-folder',
                    help='The path to the LLVM source code to build from.',
                    required=True,
                    type=Path)
parser.add_argument('-v',
                    '--show-hyperfine-commands',
                    action='store_true',
                    help='Show hyperfine commands before running them.')
parser.add_argument(
    '--skip-initial-validation',
    action='store_true',
    help=
    'Skip initial validation of Linux and LLVM sources and host environment. Only do this if it has been done before.'
)
args = parser.parse_args()

# Perform this check after argument parsing so that '-h' works for everyone.
if (MACHINE := platform.machine()) not in ('aarch64', 'x86_64'):
    print(
        'E: This script only supports aarch64 and x86_64 machines! Update this check when support for other architectures is desired.'
    )
    sys.exit(1)

# Create absolute path variables
build_folder = args.build_folder.resolve()
install_folder = args.install_folder.resolve()
linux_folder = args.kernel_folder.resolve()
llvm_folder = args.llvm_folder.resolve()

# Valid source folder is new enough for internal tc-build builder
lsm = tc_build.kernel.LinuxSourceManager()
lsm.location = linux_folder
if (linux_version := lsm.get_version()) < tc_build.kernel.KernelBuilder.MINIMUM_SUPPORTED_VERSION:
    FOUND_VERSION = '.'.join(map(str, linux_version))
    MINIMUM_VERSION = '.'.join(map(str, tc_build.kernel.KernelBuilder.MINIMUM_SUPPORTED_VERSION))
    raise RuntimeError(
        f"Supplied kernel source version ('{FOUND_VERSION}') is older than the minimum required version ('{MINIMUM_VERSION}'), provide a newer version!"
    )

# Make sure the user has hyperfine in their PATH (either from a distribution
# installation or locally build/installed)
if not shutil.which('hyperfine'):
    raise FileNotFoundError('hyperfine could not be found in PATH!')

# Hyperfine will run for a LONG time even on powerful machines so we want to
# try and catch as many potential common failure reasons up front as much as
# possible, as there will be no debugging output from hyperfine in case of
# failures.
if not linux_folder.exists():
    raise FileNotFoundError(
        f"Provided Linux kernel source folder ('{linux_folder}') does not exist?")
if not llvm_folder.exists():
    raise FileNotFoundError(f"Provided LLVM source folder ('{llvm_folder}') does not exists?")
if not Path(linux_folder, 'Makefile').exists():
    raise RuntimeError(
        f"Provided Linux kernel source folder ('{linux_folder}') does not appear to be a Linux kernel source tree?"
    )
if not Path(llvm_folder, 'llvm/CMakeLists.txt').exists():
    raise RuntimeError(
        f"Provided LLVM source folder ('{llvm_folder}') does not appear to be an LLVM source tree?")
if Path(linux_folder, '.config').exists():
    raise RuntimeError(
        f"Provided Linux kernel source folder ('{linux_folder}') is not clean! Run 'make mrproper' to ensure out of tree builds will not error."
    )

# Download GCC to install folder if not already present
tc_build.utils.print_header('Downloading GCC from kernel.org if necessary')
GCC_HOST_ARCH = {
    'aarch64': 'arm64',
    'x86_64': 'x86_64',
}[MACHINE]
GCC_VERSION = '13.2.0'
GCC_TUPLES = [
    'aarch64-linux',
    'arm-linux-gnueabi',
    'x86_64-linux',
]
(GCC_INSTALL := Path(install_folder, 'gcc', GCC_VERSION)).mkdir(exist_ok=True, parents=True)
for gcc_tuple in GCC_TUPLES:
    if (gcc_binary := Path(GCC_INSTALL, f"bin/{gcc_tuple}-gcc")).exists():
        tc_build.utils.print_info(f"{gcc_binary} found.")
        continue

    url = f"https://mirrors.edge.kernel.org/pub/tools/crosstool/files/bin/{GCC_HOST_ARCH}/{GCC_VERSION}/{GCC_HOST_ARCH}-gcc-{GCC_VERSION}-nolibc-{gcc_tuple}.tar.xz"

    tc_build.utils.print_info(
        f"Downloading and extracting {url.rsplit('/', 2)[-1]} to {GCC_INSTALL}")

    response = requests.get(url, timeout=3600)
    response.raise_for_status()

    tar_cmd = [
        'tar',
        '-C', GCC_INSTALL,
        '--extract',
        '--file=-',
        '--strip-components=2',
        '--xz',
    ]  # yapf: disable
    subprocess.run(tar_cmd, check=True, input=response.content)

# Initial build configuration
llvm_build_folder = Path(build_folder, 'llvm')
linux_build_folder = Path(build_folder, 'linux')

CHECK_TARGETS = [
    'clang',
    'lld',
    'llvm',
    'llvm-unit',
]
TARGETS = ['ARM', 'AArch64', 'X86']
BASE_BUILD_LLVM_PY_CMD = [
    shutil.which('python3'),
    BUILD_LLVM_PY,
    '--build-folder', llvm_build_folder,
    '--check-targets', *CHECK_TARGETS,
    '--llvm-folder', llvm_folder,
    '--no-ccache',
    '--quiet-cmake',
    '--targets', *TARGETS,
]  # yapf: disable

# Try to build a stage 1 LLVM build (which checks the user's host environment
# for building LLVM) then use it to build kernels from the provided source.
if not args.skip_initial_validation:
    tc_build.utils.print_header('Validating host environment and provided sources')
    print('This will build a copy of LLVM in a single stage configuration then '
          'build a series of Linux kernels with that copy of LLVM to validate '
          'the revisions of the provided LLVM and Linux source trees and the '
          'host environment for building LLVM and Linux.')

    try:
        build_llvm_py_cmd = [
            *BASE_BUILD_LLVM_PY_CMD,
            '--build-stage1-only',
        ]
        subprocess.run(build_llvm_py_cmd, check=True)

        kernel_builder = tc_build.kernel.LLVMKernelBuilder()
        kernel_builder.folders.build = Path(build_folder, 'linux')
        kernel_builder.folders.source = lsm.location
        kernel_builder.matrix = {
            'defconfig': TARGETS,
            'allmodconfig': TARGETS,
        }
        kernel_builder.toolchain_prefix = Path(llvm_build_folder, 'final')
        kernel_builder.build()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            'Validating environment by building LLVM then building kernel with just built toolchain failed! This usually means something is wrong with your LLVM or Linux kernel source or host environment.'
        ) from e

MEM = int(os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / 1024**3)
FULL_LTO_JL, THIN_LTO_JL = MEM // 30, MEM // 15

LLVM_INSTALL = Path(install_folder, 'llvm')
LLVM_MATRIX = [
    {
        'args': ['--build-stage1-only'],
        'description': {
            'full': 'Stage one only',
            'short': 'stage-one',
        },
    },
    {
        'args': [],
        'description': {
            'full': 'Default two stage build',
            'short': 'normal',
        },
    },
    {
        'args': ['--lto', 'thin'],
        'description': {
            'full': 'Two stage build with ThinLTO',
            'short': 'thinlto',
        },
    },
    {
        'args': ['--lto', 'full'],
        'description': {
            'full': 'Two stage build with LTO',
            'short': 'lto',
        },
    },
    {
        'args': ['--pgo', 'kernel-defconfig'],
        'description': {
            'full': 'Three stage build with PGO against defconfig',
            'short': 'pgo-defconfig',
        },
    },
    {
        'args': ['--pgo', 'kernel-defconfig', 'kernel-allmodconfig'],
        'description': {
            'full': 'Three stage build with PGO against defconfig and allmodconfig',
            'short': 'pgo-defconfig-allmodconfig',
        },
    },
    {
        'args': ['--lto', 'thin', '--pgo', 'kernel-defconfig'],
        'description': {
            'full': 'Three stage build with ThinLTO and PGO against defconfig',
            'short': 'pgo-defconfig-thinlto',
        },
    },
    {
        'args': ['--lto', 'full', '--pgo', 'kernel-defconfig'],
        'description': {
            'full': 'Three stage build with LTO and PGO against defconfig',
            'short': 'pgo-defconfig-lto',
        },
    },
]
if MACHINE == 'x86_64':
    LLVM_MATRIX += [
        {
            'args': ['--bolt', '--pgo', 'kernel-defconfig'],
            'description': {
                'full': 'Three stage build with BOLT and PGO against defconfig',
                'short': 'pgo-defconfig-bolt',
            },
        },
        {
            'args': ['--bolt', '--lto', 'thin', '--pgo', 'kernel-defconfig'],
            'description': {
                'full': 'Three stage build with BOLT, ThinLTO, and PGO against defconfig',
                'short': 'pgo-defconfig-bolt-thinlto',
            },
        },
        {
            'args': ['--bolt', '--lto', 'full', '--pgo', 'kernel-defconfig'],
            'description': {
                'full': 'Three stage build with BOLT, LTO, and PGO against defconfig',
                'short': 'pgo-defconfig-bolt-lto',
            },
        },
    ]

tc_build.utils.print_header('LLVM build benchmarking')

hyperfine_descriptions = []
hyperfine_cmds = []
llvm_toolchains = []
for matrix_item in LLVM_MATRIX:
    matrix_install_folder = Path(LLVM_INSTALL, matrix_item['description']['short'])

    hyperfine_descriptions.append(matrix_item['description']['full'])
    llvm_toolchains.append(Path(matrix_install_folder, 'bin'))

    build_llvm_py_cmd = [
        *BASE_BUILD_LLVM_PY_CMD,
        '--install-folder',
        matrix_install_folder,
        *matrix_item['args'],
    ]
    if '--pgo' in build_llvm_py_cmd:
        build_llvm_py_cmd += ['--linux-folder', linux_folder]
    # Avoid running OOM when linking large binaries...
    if 'full' in build_llvm_py_cmd:
        build_llvm_py_cmd += ['--defines', f"LLVM_PARALLEL_LINK_JOBS={FULL_LTO_JL}"]
    if 'thin' in build_llvm_py_cmd:
        build_llvm_py_cmd += ['--defines', f"LLVM_PARALLEL_LINK_JOBS={THIN_LTO_JL}"]

    hyperfine_cmds.append(' '.join(str(elem) for elem in build_llvm_py_cmd))

hyperfine_cmd = [
    'hyperfine',
    *[opt for elem in hyperfine_descriptions for opt in ('--command-name', elem)],
    '--export-markdown', Path(RESULTS, 'llvm.md'),
    '--prepare', f"rm -fr {llvm_build_folder}",
    '--runs', '5',
    '--shell', 'none',
    '--warmup', '1',
    *hyperfine_cmds,
]  # yapf: disable

if args.show_hyperfine_commands:
    print(f"$ {' '.join([shlex.quote(str(elem)) for elem in hyperfine_cmd])}", flush=True)
if args.dry_run:
    tc_build.utils.print_warning('Dry run requested, not running hyperfine...')
else:
    subprocess.run(hyperfine_cmd, check=True)

tc_build.utils.print_header('Linux kernel build benchmarking')

KERNEL_MATRIX = [
    {
        'arch': 'arm',
        'config': 'multi_v7_defconfig',
        'cross_compile': 'arm-linux-gnueabi-',
    },
    {
        'arch': 'arm64',
        'config': 'defconfig',
        'cross_compile': 'aarch64-linux-',
    },
    {
        'arch': 'x86_64',
        'config': 'defconfig',
        'cross_compile': 'x86_64-linux-',
    },
    {
        'arch': 'arm',
        'config': 'allmodconfig',
        'cross_compile': 'arm-linux-gnueabi-',
    },
    {
        'arch': 'arm64',
        'config': 'allmodconfig',
        'cross_compile': 'aarch64-linux-',
    },
    {
        'arch': 'x86_64',
        'config': 'allmodconfig',
        'cross_compile': 'x86_64-linux-',
    },
]
BASE_MAKE_CMD = [
    shutil.which('make'),
    '--directory',
    linux_folder,
    '--keep-going',
    '--jobs',
    len(os.sched_getaffinity(0)),
    '--silent',
]
BASE_MAKE_VARS = {
    'KCFLAGS': '-Wno-error',
    'O': linux_build_folder,
}
# The kernel.org toolchains sometimes have issues with building and linking the
# GCC plugins. They should not make a huge impact on compile time performance,
# so disable them for the benchmarking to avoid these build errors.
if not (allmod_config := Path(build_folder, '.allmod.config')).exists():
    allmod_config.parent.mkdir(exist_ok=True)
    allmod_config.write_text('CONFIG_GCC_PLUGINS=n\n', encoding='utf-8')
for matrix_item in KERNEL_MATRIX:
    matrix_make_vars = {
        'ARCH': matrix_item['arch'],
        **BASE_MAKE_VARS,
    }
    toolchain_make_vars = [
        {
            'CROSS_COMPILE': Path(GCC_INSTALL, 'bin', matrix_item['cross_compile']),
            'KCONFIG_ALLCONFIG': allmod_config,
        },
        *[{'LLVM': f"{llvm_tc}/"} for llvm_tc in llvm_toolchains],
    ]  # yapf: disable

    hyperfine_cmd = [
        'hyperfine',
        '--command-name', f"GCC {GCC_VERSION}",
        *[opt for elem in hyperfine_descriptions for opt in ('--command-name', f"LLVM ({elem})")],
        '--export-markdown', Path(RESULTS, f"{matrix_item['arch']}-{matrix_item['config']}.md"),
        '--prepare', f"rm -fr {BASE_MAKE_VARS['O']}",
        '--runs', '10' if 'defconfig' in matrix_item['config'] else '5',
        '--shell', 'none',
        '--warmup', '1',
    ]  # yapf: disable

    for tc_make_vars in toolchain_make_vars:
        make_variables = {**matrix_make_vars, **tc_make_vars}
        make_command = [
            *BASE_MAKE_CMD,
            *[f"{key}={make_variables[key]}" for key in sorted(make_variables)],
            matrix_item['config'],
            'all',
        ]
        hyperfine_cmd.append(' '.join(str(elem) for elem in make_command))

    tc_build.utils.print_info(f"Benchmarking ARCH={matrix_item['arch']} {matrix_item['config']}...")
    if args.show_hyperfine_commands:
        print(f"$ {' '.join([shlex.quote(str(elem)) for elem in hyperfine_cmd])}", flush=True)
    if args.dry_run:
        tc_build.utils.print_warning('Dry run requested, not running hyperfine...')
    else:
        subprocess.run(hyperfine_cmd, check=True)
