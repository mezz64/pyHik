# Copyright (c) 2016-2026 John Mihalic <https://github.com/mezz64>
# Licensed under the MIT license.

# Used this guide to create module
# http://peterdowns.com/posts/first-time-with-pypi.html

# git tag 0.1 -m "0.1 release"
# git push --tags origin master
#
# Upload to PyPI Live
# python setup.py register -r pypi
# python setup.py sdist upload -r pypi


from setuptools import setup

setup(
    name='pyHik',
    packages=['pyhik'],
    version='0.4.1',
    description='Python API for Hikvision cameras and NVRs - event streaming, ISAPI access, and device control.',
    author='John Mihalic',
    author_email='mezz64@users.noreply.github.com',
    license='MIT',
    url='https://github.com/mezz64/pyhik',
    download_url='https://github.com/mezz64/pyhik/tarball/0.4.1',
    keywords=['hik', 'hikvision', 'event stream', 'events', 'api wrapper', 'homeassistant', 'isapi', 'nvr', 'camera'],
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
    ],
    python_requires='>=3.9',
    install_requires=[
        'requests>=2.20.0',
    ],
    extras_require={
        'isapi': ['xmltodict>=0.13.0'],
    },
)
