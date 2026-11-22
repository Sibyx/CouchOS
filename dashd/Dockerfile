# dashd for the Pi: built on Debian trixie arm64, so it links against the
# same glibc as Raspberry Pi OS. Run through build.sh.
FROM rust:1-slim-trixie AS build
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN cargo build --release --locked \
 && mkdir /out \
 && cp target/release/dashd /out/dashd
