TEST_ARTIFACTS ?= /tmp/coverage

.PHONY: install dev_install lint format static_type_check style_check test

install:
	python3 -m pip install --upgrade pip setuptools
	python3 -m pip install -r requirements.txt

dev_install: install
	python3 -m pip install -r dev-requirements.txt

lint:
	ruff check --select E9,F media_downloader.py module utils scripts tests

format:
	ruff format --check media_downloader.py module utils scripts tests

static_type_check:
	mypy media_downloader.py utils module scripts --ignore-missing-imports

style_check: lint static_type_check

test:
	pytest --cov media_downloader --cov module --cov utils \
		--cov-report term-missing \
		--cov-report html:${TEST_ARTIFACTS} \
		--junit-xml=${TEST_ARTIFACTS}/media-downloader.xml \
		tests/
