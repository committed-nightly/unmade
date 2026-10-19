# An example project makefile, with the kind of dead lines that accumulate
# in a real one. Nothing here is contrived syntax -- every mistake below is a
# shape that survives in repositories because it looks right.

CC = gcc
CFLAGS = -Wall -Wextra
CFLAGS += -O2
CFLAGS += -g

# Moved from /usr/local to /opt a while ago. The old line stayed.
PREFIX = /usr/local
PREFIX = /opt/example

# Meant to be "default to 1.2.0 unless someone overrides it". `?=` only
# assigns when the variable is unset, and the line above set it.
VERSION = 1.2.0
VERSION ?= 0.0.0

BUILD_DIR = build

# Optional local overrides. There is no local.mk, and `-include` does not
# say so.
-include local.mk

# Turned on by TOOLCHAIN=clang. Nothing sets TOOLCHAIN.
ifeq ($(TOOLCHAIN),clang)
CC = clang
SANITISE = -fsanitize=address
endif

.PHONY: all clean test instal

all: $(BUILD_DIR)/app

$(BUILD_DIR)/app: main.c
	@mkdir -p $(BUILD_DIR)
	$(CC) $(CFLAGS) -DVERSION=\"$(VERSION)\" -o $@ $<

test: all
	@echo "running the unit tests"

clean:
	@rm -rf $(BUILD_DIR)

install: all
	@install -D $(BUILD_DIR)/app $(PREFIX)/bin/app

# Added during a rebase, on the assumption the rule above had gone.
test: all
	@echo "running the full suite"
