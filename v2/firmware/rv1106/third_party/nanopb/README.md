# nanopb 0.4.9 (vendored)

Runtime only: `pb.h`, `pb_common.[ch]`, `pb_encode.[ch]`, `pb_decode.[ch]`,
verbatim from the nanopb 0.4.9 release, plus its zlib licence
(`LICENSE.txt`). The generator is NOT vendored — `scripts/regenerate-proto.sh`
runs it from a pinned package in a throwaway environment.

Vendored rather than fetched because the daemon must build from this
repository plus a toolchain and nothing else, and because the version is part
of the wire result: nanopb's encoder is one half of the byte-identity gate
(test E2), so bumping it is a decision that has to be made deliberately and
re-verified, not a dependency resolver's choice on the day of a build.
