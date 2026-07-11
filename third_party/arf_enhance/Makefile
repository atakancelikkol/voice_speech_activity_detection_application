CC ?= clang
CFLAGS ?= -O2 -Wall -Wextra -fPIC

libarfenhance.dylib: arf_enhance.c arf_enhance.h
	$(CC) $(CFLAGS) -dynamiclib -install_name @rpath/libarfenhance.dylib -o $@ arf_enhance.c -lm

.PHONY: clean
clean:
	rm -f libarfenhance.dylib
