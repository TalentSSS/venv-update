from __future__ import absolute_import
from __future__ import print_function
from __future__ import unicode_literals

import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from errno import ECONNREFUSED

import pytest
import six
from ephemeral_port_reserve import reserve

from testing import run
from testing import TOP
from venv_update import colorize


ENV_WHITELIST = (
    # allows coverage of subprocesses
    'COVERAGE_PROCESS_START',
    # used in the configuration of coverage
    'TOP',
    # let's not fill up the root partition, please
    'TMPDIR',
    # these help my debugger not freak out
    'HOME',
    'TERM',
)


@pytest.yield_fixture(autouse=True)
def fixed_environment_variables():
    orig_environ = os.environ.copy()

    for var in dict(os.environ):
        if var not in ENV_WHITELIST:
            del os.environ[var]

    # disable casual interaction with python.org
    os.environ['PIP_INDEX_URL'] = '(total garbage)'
    os.environ['PYTHONWARNINGS'] = 'ignore:Support for Python 3.0-3.2 has been dropped.:UserWarning'

    # Fixes argparse help tests.
    os.environ['COLUMNS'] = '1000'

    # normalize $PATH
    from sys import executable
    from os import defpath
    from os.path import dirname
    assert defpath.startswith(':')
    os.environ['PATH'] = dirname(executable) + defpath
    yield
    os.environ.clear()
    os.environ.update(orig_environ)


@pytest.yield_fixture
def tmpdir(tmpdir):
    """override tmpdir to provide a $HOME and $TMPDIR"""
    home = tmpdir.ensure('home', dir=True)
    tmp = tmpdir.ensure('tmp', dir=True)

    orig_environ = os.environ.copy()
    os.environ['HOME'] = str(home)
    os.environ['TMPDIR'] = str(tmp)

    with tmpdir.as_cwd():  # TODO: remove all the tmpdir.chdir()
        yield tmpdir

    os.environ.clear()
    os.environ.update(orig_environ)


@pytest.yield_fixture(scope='session')
def pypi_packages():
    package_temp = TOP.join('build/test-packages')
    with TOP.as_cwd():
        run(
            sys.executable,
            'tests/testing/make_sdists.py',
            'tests/testing/packages',
            '.',  # we need venv-update to be installable too
            str(package_temp),
        )

    yield package_temp


@pytest.yield_fixture(scope='session')
def pypi_port():
    yield reserve()


@contextmanager
def start_pypi_server(packages, port, pypi_fallback):
    port = str(port)
    cmd = (
        'pypi-server', '-v',
        '-i', '127.0.0.1',
        '-p', port,
        # Default fallback is HTTP, which is no longer supported.
        # TODO: revert after a new pypiserver is released with this patch:
        # https://github.com/pypiserver/pypiserver/pull/182
        '--fallback-url', 'https://pypi.python.org/simple',
    )
    if not pypi_fallback:
        cmd += ('--disable-fallback',)
    cmd += (str(packages),)
    print(colorize(cmd))
    server = subprocess.Popen(cmd, cwd=str(TOP), stderr=1)

    limit = 10
    poll = .1
    pypi_url = 'http://localhost:' + str(port)
    while True:
        if server.poll() is not None:
            raise AssertionError('pypi ended! (code %i)' % server.returncode)
        elif service_up(pypi_url):
            break
        elif limit > 0:
            time.sleep(poll)
            limit -= poll
        else:
            raise AssertionError('pypi server never became ready!')

    os.environ['PIP_INDEX_URL'] = pypi_url + '/simple'
    try:
        yield pypi_url
    finally:
        server.terminate()
        server.wait()


@pytest.yield_fixture
def pypi_server(pypi_packages, pypi_port):
    with start_pypi_server(pypi_packages, pypi_port, False) as pypi_url:
        yield pypi_url


@pytest.yield_fixture
def pypi_server_with_fallback(pypi_packages, pypi_port):
    with start_pypi_server(pypi_packages, pypi_port, True) as pypi_url:
        yield pypi_url


def ioerror_to_errno(error):  # :pragma:nocover:  all of these cases are exceptional and quite rare
    if isinstance(error.errno, int):
        return error.errno
    # urllib throws an IOError with a string in the errno slot -.-
    elif len(error.args) > 1 and isinstance(error.args[1], socket.error):
        return error.args[1].errno
    elif len(error.args) == 1 and isinstance(error.args[0], socket.error):
        return error.args[0].errno
    elif hasattr(error, 'code') and isinstance(error.code, int):
        return error.code
    else:
        raise ValueError('Could not find error number: %r' % error)


def service_up(url):
    try:
        response = six.moves.urllib.request.urlopen(url)
    except IOError as error:
        if ioerror_to_errno(error) in (ECONNREFUSED, 404):
            print('pypi not up:', error)
            return False
        else:
            raise

    print('pypi response:', response)
    return response.getcode() == 200


def pytest_assertrepr_compare(config, op, left, right):  # TODO: unit-test :pragma:nocover:
    if op == 'in' and '\n' in left:
        # Convert 'in' comparisons to '==' comparisons, for more usable error messaging.
        # Truncate the right-hand-side such that it has the longest common prefix with the LHS,
        # and the longest common suffix as well.
        # Given the diff of the two, this should pinpoint the difference.
        beginning = end = None
        for i in range(len(left)):
            if beginning and end:
                break

            if beginning is None and left[:i + 1] not in right:
                beginning = left[:i]

            if end is None and left[-i - 1:] not in right:
                end = left[-i:]

        right = right.split(beginning, 1)[-1].rsplit(end, 1)[0]
        right = ''.join((beginning, right, end))

        from _pytest.assertion.util import assertrepr_compare
        return assertrepr_compare(config, '==', left, right)
