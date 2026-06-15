# maat-kerneld — the deterministic spine (D20). Multi-stage: build the release binary, ship it
# on a slim runtime. Migrations are embedded at compile time via sqlx::migrate!() (they live in
# crates/maat-kerneld/migrations and travel inside the binary), and every query is runtime-checked
# (sqlx::query, not the query! macro), so no DATABASE_URL is needed at build time.
FROM rust:1-bookworm AS build
WORKDIR /src
# Copy the whole workspace (kernel + kerneld + migrations) and build only the daemon.
COPY . .
RUN cargo build --release -p maat-kerneld

FROM debian:bookworm-slim
# rustls (used by sqlx + async-nats here) verifies TLS against the system root store.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/target/release/maat-kerneld /usr/local/bin/maat-kerneld
# DATABASE_URL + NATS_URL come from the environment (compose env_file). The daemon runs the
# embedded migrations on startup, then subscribes to maat.events.> as the single writer.
ENTRYPOINT ["maat-kerneld"]
