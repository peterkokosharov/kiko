#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
"""Setup script for kikolib (installed as kiko-dev)."""

from setuptools import setup, find_packages

setup(
    name="kikolib",
    version="0.1.0",
    author="Peter Kokosharov",
    author_email="peter.kokosharov@gmail.com",
    description="stdlib-only library for kiko-lite proxy",
    license="MIT",
    packages=find_packages(include=["kikolib", "kikolib.*"]),
    python_requires=">=3.11",
    install_requires=[],
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Operating System :: POSIX :: Linux",
    ],
)