
.PHONY: FORCE

$(shell mkdir -p build)

build/luke: FORCE
	cd luke && cargo build --target=x86_64-unknown-linux-musl --release
	cp luke/target/x86_64-unknown-linux-musl/release/luke $@
	strip $@
