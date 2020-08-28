"""
The matplotlib build options can be modified with a setup.cfg file. See
setup.cfg.template for more information.
"""

# NOTE: This file must remain Python 2 compatible for the foreseeable future,
# to ensure that we error out properly for people with outdated setuptools
# and/or pip.
import sys

min_version = (3, 7)

if sys.version_info < min_version:
    error = """
Beginning with Matplotlib 3.1, Python {0} or above is required.
You are using Python {1}.

This may be due to an out of date pip.

Make sure you have pip >= 9.0.1.
""".format('.'.join(str(n) for n in min_version),
           '.'.join(str(n) for n in sys.version_info[:3]))
    sys.exit(error)

import os
from pathlib import Path
import shutil
import subprocess

from setuptools import setup, find_packages, Extension
from setuptools.command.build_ext import build_ext as BuildExtCommand
from setuptools.command.test import test as TestCommand

# The setuptools version of sdist adds a setup.cfg file to the tree.
# We don't want that, so we simply remove it, and it will fall back to
# vanilla distutils.
try:
    from setuptools.command import sdist
except ImportError:
    pass
else:
    del sdist.sdist.make_release_tree

from distutils.errors import CompileError
from distutils.dist import Distribution

import setupext
from setupext import print_raw, print_status

# Get the version from versioneer
import versioneer
__version__ = versioneer.get_version()


# These are the packages in the order we want to display them.
mpl_packages = [
    setupext.Matplotlib(),
    setupext.Python(),
    setupext.Platform(),
    setupext.FreeType(),
    setupext.SampleData(),
    setupext.Tests(),
    setupext.BackendMacOSX(),
    ]


# From https://bugs.python.org/issue26689
def has_flag(self, flagname):
    """Return whether a flag name is supported on the specified compiler."""
    import tempfile
    with tempfile.NamedTemporaryFile('w', suffix='.cpp') as f:
        f.write('int main (int argc, char **argv) { return 0; }')
        try:
            self.compile([f.name], extra_postargs=[flagname])
        except CompileError:
            return False
    return True


class NoopTestCommand(TestCommand):
    def __init__(self, dist):
        print("Matplotlib does not support running tests with "
              "'python setup.py test'. Please run 'pytest'.")


class BuildExtraLibraries(BuildExtCommand):
    def finalize_options(self):
        self.distribution.ext_modules[:] = [
            ext
            for package in good_packages
            for ext in package.get_extensions()
        ]
        super().finalize_options()

    def add_optimization_flags(self):
        """
        Add optional optimization flags to extension.

        This adds flags for LTO and hidden visibility to both compiled
        extensions, and to the environment variables so that vendored libraries
        will also use them. If the compiler does not support these flags, then
        none are added.
        """

        env = os.environ.copy()
        if not setupext.config.getboolean('libs', 'enable_lto', fallback=True):
            return env
        if sys.platform == 'win32':
            return env

        cppflags = []
        if 'CPPFLAGS' in os.environ:
            cppflags.append(os.environ['CPPFLAGS'])
        cxxflags = []
        if 'CXXFLAGS' in os.environ:
            cxxflags.append(os.environ['CXXFLAGS'])
        ldflags = []
        if 'LDFLAGS' in os.environ:
            ldflags.append(os.environ['LDFLAGS'])

        if has_flag(self.compiler, '-fvisibility=hidden'):
            for ext in self.extensions:
                ext.extra_compile_args.append('-fvisibility=hidden')
            cppflags.append('-fvisibility=hidden')
        if has_flag(self.compiler, '-fvisibility-inlines-hidden'):
            for ext in self.extensions:
                if self.compiler.detect_language(ext.sources) != 'cpp':
                    continue
                ext.extra_compile_args.append('-fvisibility-inlines-hidden')
            cxxflags.append('-fvisibility-inlines-hidden')
        ranlib = 'RANLIB' in env
        if not ranlib and self.compiler.compiler_type == 'unix':
            try:
                result = subprocess.run(self.compiler.compiler +
                                        ['--version'],
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT,
                                        universal_newlines=True)
            except Exception as e:
                pass
            else:
                version = result.stdout.lower()
                if 'gcc' in version:
                    ranlib = shutil.which('gcc-ranlib')
                elif 'clang' in version:
                    if sys.platform == 'darwin':
                        ranlib = True
                    else:
                        ranlib = shutil.which('llvm-ranlib')
        if ranlib and has_flag(self.compiler, '-flto'):
            for ext in self.extensions:
                ext.extra_compile_args.append('-flto')
            cppflags.append('-flto')
            ldflags.append('-flto')
            # Needed so FreeType static library doesn't lose its LTO objects.
            if isinstance(ranlib, str):
                env['RANLIB'] = ranlib

        env['CPPFLAGS'] = ' '.join(cppflags)
        env['CXXFLAGS'] = ' '.join(cxxflags)
        env['LDFLAGS'] = ' '.join(ldflags)

        return env

    def build_extensions(self):
        # Remove the -Wstrict-prototypes option, it's not valid for C++.  Fixed
        # in Py3.7 as bpo-5755.
        try:
            self.compiler.compiler_so.remove('-Wstrict-prototypes')
        except (ValueError, AttributeError):
            pass

        env = self.add_optimization_flags()
        for package in good_packages:
            package.do_custom_build(env)
        return super().build_extensions()


cmdclass = versioneer.get_cmdclass()
cmdclass['test'] = NoopTestCommand
cmdclass['build_ext'] = BuildExtraLibraries


package_data = {}  # Will be filled below by the various components.

# If the user just queries for information, don't bother figuring out which
# packages to build or install.
if not (any('--' + opt in sys.argv
            for opt in Distribution.display_option_names + ['help'])
        or 'clean' in sys.argv):
    # Go through all of the packages and figure out which ones we are
    # going to build/install.
    print_raw()
    print_raw("Edit setup.cfg to change the build options; "
              "suppress output with --quiet.")
    print_raw()
    print_raw("BUILDING MATPLOTLIB")

    good_packages = []
    for package in mpl_packages:
        try:
            message = package.check()
        except setupext.Skipped as e:
            print_status(package.name, "no  [{e}]".format(e=e))
            continue
        if message is not None:
            print_status(package.name,
                         "yes [{message}]".format(message=message))
        good_packages.append(package)

    print_raw()

    # Now collect all of the information we need to build all of the packages.
    for package in good_packages:
        # Extension modules only get added in build_ext, as numpy will have
        # been installed (as setup_requires) at that point.
        data = package.get_package_data()
        for key, val in data.items():
            package_data.setdefault(key, [])
            package_data[key] = list(set(val + package_data[key]))

    # Write the default matplotlibrc file
    with open('matplotlibrc.template') as fd:
        template_lines = fd.read().splitlines(True)
    backend_line_idx, = [  # Also asserts that there is a single such line.
        idx for idx, line in enumerate(template_lines)
        if line.startswith('#backend:')]
    if setupext.options['backend']:
        template_lines[backend_line_idx] = (
            'backend: {}'.format(setupext.options['backend']))
    with open('lib/matplotlib/mpl-data/matplotlibrc', 'w') as fd:
        fd.write(''.join(template_lines))

setup(  # Finally, pass this all along to distutils to do the heavy lifting.
    name="matplotlib",
    version=__version__,
    description="Python plotting package",
    author="John D. Hunter, Michael Droettboom",
    author_email="matplotlib-users@python.org",
    url="https://matplotlib.org",
    download_url="https://matplotlib.org/users/installing.html",
    project_urls={
        'Documentation': 'https://matplotlib.org',
        'Source Code': 'https://github.com/matplotlib/matplotlib',
        'Bug Tracker': 'https://github.com/matplotlib/matplotlib/issues',
        'Forum': 'https://discourse.matplotlib.org/',
        'Donate': 'https://numfocus.org/donate-to-matplotlib'
    },
    long_description=Path("README.rst").read_text(encoding="utf-8"),
    long_description_content_type="text/x-rst",
    license="PSF",
    platforms="any",
    classifiers=[
        'Development Status :: 5 - Production/Stable',
        'Framework :: Matplotlib',
        'Intended Audience :: Science/Research',
        'Intended Audience :: Education',
        'License :: OSI Approved :: Python Software Foundation License',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Topic :: Scientific/Engineering :: Visualization',
    ],

    package_dir={"": "lib"},
    packages=find_packages("lib"),
    namespace_packages=["mpl_toolkits"],
    py_modules=["pylab"],
    # Dummy extension to trigger build_ext, which will swap it out with
    # real extensions that can depend on numpy for the build.
    ext_modules=[Extension("", [])],
    package_data=package_data,

    python_requires='>={}'.format('.'.join(str(n) for n in min_version)),
    setup_requires=[
        "numpy>=1.15",
    ],
    install_requires=[
        "cycler>=0.10",
        "kiwisolver>=1.0.1",
        "numpy>=1.16",
        "pillow>=6.2.0",
        "pyparsing>=2.2.1",
        "python-dateutil>=2.7",
    ],

    cmdclass=cmdclass,
)
