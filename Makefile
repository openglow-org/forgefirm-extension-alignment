# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# SPDX-License-Identifier: MIT
#
# org.openglow.alignment: a page, and nothing that runs on the machine.
# `make stage` lays out what the package ships - the manifest and the
# page, and nothing of the repository's own files - in build/pkg, and
# `make pack` packs it with ffx (KEY=<file.priv> signs it). FFX is
# forgeext's tools/ffx: a sibling checkout by default, and the kit's in CI.

FFX ?= ../forgeext/tools/ffx

stage: manifest.json ui/index.html
	rm -rf build/pkg
	mkdir -p build/pkg/ui
	cp manifest.json build/pkg/
	cp ui/index.html build/pkg/ui/

pack: stage
	python3 $(FFX) pack build/pkg $(if $(KEY),--key $(KEY)) --out build/alignment.ffx

clean:
	rm -rf build

.PHONY: stage pack clean
