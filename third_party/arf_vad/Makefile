CC ?= clang
CFLAGS ?= -O2 -Wall -Wextra -fPIC

libarfvad.dylib: arf_vad.c arf_vad.h
	$(CC) $(CFLAGS) -dynamiclib -install_name @rpath/libarfvad.dylib -o $@ arf_vad.c

.PHONY: clean
clean:
	rm -f libarfvad.dylib
