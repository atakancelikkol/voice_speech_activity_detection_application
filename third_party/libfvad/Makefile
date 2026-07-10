CC ?= clang
CFLAGS ?= -O2 -Wall -Wextra -fPIC
CPPFLAGS = -Iinclude -Isrc

SRCS = src/fvad.c \
       src/signal_processing/division_operations.c \
       src/signal_processing/energy.c \
       src/signal_processing/get_scaling_square.c \
       src/signal_processing/resample_48khz.c \
       src/signal_processing/resample_by_2_internal.c \
       src/signal_processing/resample_fractional.c \
       src/signal_processing/spl_inl.c \
       src/vad/vad_core.c \
       src/vad/vad_filterbank.c \
       src/vad/vad_gmm.c \
       src/vad/vad_sp.c

libfvad.dylib: $(SRCS) include/fvad.h
	$(CC) $(CFLAGS) $(CPPFLAGS) -dynamiclib -install_name @rpath/libfvad.dylib -o $@ $(SRCS)

.PHONY: clean
clean:
	rm -f libfvad.dylib
