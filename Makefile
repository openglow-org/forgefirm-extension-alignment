# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# SPDX-License-Identifier: MIT
#
# org.openglow.alignment: a page, and the service that keeps the tool
# (bin/run.py, Python, with the SDK's lib/ffx.py). `make test` runs the
# service's test. `make stage` lays out what the package ships - the
# manifest, the page, the service and its library, and nothing of the
# repository's own files - in build/pkg, and `make pack` packs it with ffx
# (KEY=<file.priv> signs it). FFX is forgeext's tools/ffx: a sibling
# checkout by default, and the kit's in CI.

FFX ?= ../forgeext/tools/ffx

test:
	PYTHONDONTWRITEBYTECODE=1 python3 tests/service_test.py

stage: manifest.json ui/index.html bin/run.py lib/ffx.py
	rm -rf build/pkg
	mkdir -p build/pkg/ui build/pkg/bin build/pkg/lib
	cp manifest.json build/pkg/
	cp ui/index.html build/pkg/ui/
	cp bin/run.py build/pkg/bin/
	cp lib/ffx.py build/pkg/lib/

pack: stage
	python3 $(FFX) pack build/pkg $(if $(KEY),--key $(KEY)) --out build/alignment.ffx

clean:
	rm -rf build

.PHONY: test stage pack clean
