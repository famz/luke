
.PHONY: FORCE

$(shell mkdir -p build)

build/luke: FORCE
	cd luke && cargo build --target=x86_64-unknown-linux-musl --release
	cp luke/target/x86_64-unknown-linux-musl/release/luke $@
	strip $@

unittest: FORCE
	cd luke && cargo test

test: FORCE build/vmlinuz build/initrd
	qemu-system-x86_64 -enable-kvm -smp cores=1 -m 2g \
		-nographic \
		-nodefaults \
		-kernel build/vmlinuz \
		-initrd build/initrd \
		-append 'console=ttyS0' -serial stdio

build/vmlinuz: build/linux/.config
	$(MAKE) -C build/linux bzImage
	mv build/linux/arch/x86/boot/bzImage $@

build/linux/.config: build/linux/.git
	$(MAKE) -C build/linux defconfig kvm_guest.config

build/linux/.git:
		git clone --depth=1 https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git -b v6.6.9 build/linux

build/initrd: build/luke
	rm -rf build/initrd-build
	mkdir -p build/initrd-build
	cp build/luke build/initrd-build/init
	(cd build/initrd-build && find . | cpio --quiet -o -H newc | gzip -9) > $@
