.PHONY: install
install:
	pip install -v ".[test]"

.PHONY: editable
editable:
	pip install -v --no-build-isolation -e ".[test]"

.PHONY: cpp
cpp:
	cmake -G Ninja -S . -B cpp_build -DCMAKE_BUILD_TYPE=Debug -DCMAKE_VERBOSE_MAKEFILE=On
	cmake --build cpp_build

.PHONY: clean
clean:
	rm -rf build
	rm -rf cpp_build
	pip uninstall forest_inventory_pipeline

# Everything CI and the release build check, in one command; run it before tagging a release.
# LOG_LEVEL=warn matches CI (a test that reads log text must pass that way too).
# our modules (the older upstream files are not held to the lint rules)
P = python/forest_inventory_pipeline
LINT = $(P)/app $(P)/plugins $(P)/inventory.py $(P)/records.py $(P)/errors.py \
       $(P)/calibration.py $(P)/synthetic.py $(P)/traits/dbh/robust_fit.py $(P)/fip.py \
       $(P)/georef.py $(P)/merge.py $(P)/tiling.py $(P)/profiles.py $(P)/structures.py \
       $(P)/exclusions.py $(P)/drive.py $(P)/guards.py $(P)/rejections.py $(P)/units.py $(P)/crs.py tests/inventory plugins

.PHONY: lint
lint:
	ruff check $(LINT)

.PHONY: check
check: lint
	LOG_LEVEL=warn python -m pytest -q tests -p no:cacheprovider
	python scripts/evaluation/benchmark_synthetic.py
