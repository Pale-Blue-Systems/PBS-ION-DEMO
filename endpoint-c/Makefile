# Endpoint B (Earth, C) of the PBS ION demo.
#
#   make            build libpbs.a, pbs_tool, test_pbs and pbs_earth
#   make test       run the unit tests (every vector in docs/test-vectors.json
#                   plus negative cases); does not need ION
#   make asan       build and run the unit tests with AddressSanitizer and
#                   UndefinedBehaviorSanitizer
#   make clean
#
# ION_PREFIX points at an ION 4.2.0 install (ion/build_ion.sh puts it in
# build/ion at the repository root). Only pbs_earth needs it.

ROOT        := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/..)
ION_PREFIX  ?= $(ROOT)/build/ion
VECTORS     ?= $(ROOT)/docs/test-vectors.json
BUILD       ?= build

CC          ?= cc
WARN        := -Wall -Wextra -Werror -Wshadow -Wstrict-prototypes -Wmissing-prototypes \
               -Wformat=2 -Wvla
CFLAGS      ?= -O2 -g
LDFLAGS     ?=
ALL_CFLAGS   = -std=c11 -D_GNU_SOURCE $(WARN) $(CFLAGS)
CRYPTO_LIBS ?= -lcrypto

# ION's installed platform.h defaults SPACE_ORDER to 2 (32-bit addresses);
# ION itself is compiled with -DSPACE_ORDER=3 on 64-bit hosts. A program
# that includes bp.h must use the same value or its struct layouts differ.
LONG_BIT    := $(shell getconf LONG_BIT)
ifeq ($(LONG_BIT),64)
ION_SPACE_ORDER := 3
else
ION_SPACE_ORDER := 2
endif
ION_CFLAGS  := -isystem $(ION_PREFIX)/include -DSPACE_ORDER=$(ION_SPACE_ORDER)
ION_LIBS    := -L$(ION_PREFIX)/lib -Wl,-rpath,$(ION_PREFIX)/lib -lbp -lici -lm -lpthread

LIB_SRC     := src/pbs.c src/json.c src/earth.c
LIB_OBJ     := $(patsubst src/%.c,$(BUILD)/%.o,$(LIB_SRC))
HDRS        := $(wildcard src/*.h)

.PHONY: all lib tools test asan clean check-ion

all: lib tools $(BUILD)/test_pbs $(BUILD)/pbs_earth

lib: $(BUILD)/libpbs.a
tools: $(BUILD)/pbs_tool

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/%.o: src/%.c $(HDRS) | $(BUILD)
	$(CC) $(ALL_CFLAGS) -c -o $@ $<

$(BUILD)/libpbs.a: $(LIB_OBJ)
	rm -f $@
	ar rcs $@ $^

$(BUILD)/pbs_tool: src/pbs_tool.c $(BUILD)/libpbs.a $(HDRS)
	$(CC) $(ALL_CFLAGS) $(LDFLAGS) -o $@ src/pbs_tool.c $(BUILD)/libpbs.a $(CRYPTO_LIBS)

$(BUILD)/test_pbs: tests/test_pbs.c $(BUILD)/libpbs.a $(HDRS)
	$(CC) $(ALL_CFLAGS) $(LDFLAGS) -Isrc -o $@ tests/test_pbs.c $(BUILD)/libpbs.a $(CRYPTO_LIBS) -lm

check-ion:
	@test -f $(ION_PREFIX)/include/bp.h || \
	  { echo "ION headers not found in $(ION_PREFIX)/include; run ion/build_ion.sh or set ION_PREFIX" >&2; exit 1; }

$(BUILD)/pbs_earth: src/pbs_earth.c $(BUILD)/libpbs.a $(HDRS) | check-ion
	$(CC) $(ALL_CFLAGS) $(ION_CFLAGS) $(LDFLAGS) -o $@ src/pbs_earth.c $(BUILD)/libpbs.a $(ION_LIBS) $(CRYPTO_LIBS)

test: $(BUILD)/test_pbs $(BUILD)/pbs_tool
	$(BUILD)/test_pbs $(VECTORS)
	./tests/test_pbs_tool.sh $(BUILD)/pbs_tool $(VECTORS)

asan:
	$(MAKE) BUILD=build/asan CFLAGS="-O1 -g -fno-omit-frame-pointer -fsanitize=address,undefined -fno-sanitize-recover=all" \
	  LDFLAGS="-fsanitize=address,undefined" build/asan/test_pbs build/asan/pbs_tool
	ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=print_stacktrace=1 build/asan/test_pbs $(VECTORS)
	ASAN_OPTIONS=detect_leaks=1 ./tests/test_pbs_tool.sh build/asan/pbs_tool $(VECTORS)

clean:
	rm -rf build
