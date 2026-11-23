"""Packaging metadata for the installer entry point.

The application is normally run from a checkout (``python media_downloader.py``)
or from the Docker image, but ``pip install .`` used to produce a broken
installation: only ``media_downloader.py`` was installed while every module and
template it imports was left behind. ``find_packages`` + ``package_data`` make
the installed tree complete, and the requirement floor matches what the pinned
dependencies actually support (Flask 3.1 / Werkzeug 3.1 need >= 3.9).
"""

from setuptools import find_packages, setup

from utils import __version__

setup(
    name="tg-dl",
    version=__version__,
    author="QCEnjoyLL",
    description=(
        "Telegram media downloader with bot, Web UI and a fixed rclone "
        "upload/delete pipeline"
    ),
    url="https://github.com/QCEnjoyLL/tg-dl",
    download_url="https://github.com/QCEnjoyLL/tg-dl/releases/latest",
    py_modules=["media_downloader"],
    packages=find_packages(include=["module", "module.*", "utils", "utils.*"]),
    package_data={
        "module": [
            "templates/*.html",
            "static/css/*.css",
            "static/js/*.js",
        ]
    },
    include_package_data=True,
    install_requires=[
        # PEP 508 direct reference: a bare URL is valid in requirements.txt but
        # rejected as packaging metadata. Same commit + hash as that file.
        "Pyrogram @ https://github.com/tangyoha/pyrogram/archive/"
        "51a100c5e2745471ee89c1dd96dd69962973108b.zip"
        "#sha256=30e55236a741deec461a952fec638e2857e4cf7bb2d8c6616d41fd2e4e0685ca",
        "rich==12.5.1",
        "PyTgCrypto==1.2.6",
        "loguru==0.6.0",
        "Werkzeug==3.1.9",
        "flask==3.1.3",
        "ply==3.11",
        "ruamel.yaml==0.17.21",
        "flask-login==0.6.3",
    ],
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Environment :: Console",
        "Operating System :: OS Independent",
        "Intended Audience :: Developers",
        "Intended Audience :: End Users/Desktop",
        "Intended Audience :: Science/Research",
        "License :: OSI Approved :: MIT License",
        "Natural Language :: English",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Topic :: Internet",
        "Topic :: Communications",
        "Topic :: Communications :: Chat",
        "Topic :: Software Development :: Libraries",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
    project_urls={
        "Tracker": "https://github.com/QCEnjoyLL/tg-dl/issues",
        "Source": "https://github.com/QCEnjoyLL/tg-dl",
    },
    python_requires=">=3.9",
)
