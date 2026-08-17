# Changelog

## [0.2.0](https://github.com/jnslmk/baumarkt-mcp/compare/baumarkt-mcp-v0.1.1...baumarkt-mcp-v0.2.0) (2026-08-17)


### Features

* add the browser pool and the shared product model ([5e516f4](https://github.com/jnslmk/baumarkt-mcp/commit/5e516f4dcfbd2d36d6ccf341b295a659381e7c55))
* **bauhaus:** add the browser-free-response BAUHAUS adapter ([5742c75](https://github.com/jnslmk/baumarkt-mcp/commit/5742c753972bb63e854230a334fbcb86d731511f))
* **globus:** add the GLOBUS BAUMARKT adapter ([3f9be7a](https://github.com/jnslmk/baumarkt-mcp/commit/3f9be7a81c68448cf8c01cf2e4f978ec94d77455))
* **hornbach:** add the HORNBACH adapter ([78e997c](https://github.com/jnslmk/baumarkt-mcp/commit/78e997cdee60dfa7e3566d4087c999743f6abe65))
* **obi:** add the browser-free OBI adapter ([98e1253](https://github.com/jnslmk/baumarkt-mcp/commit/98e1253098f013563e8794adb0a333226331a99f))
* scaffold baumarkt-mcp project ([74255e7](https://github.com/jnslmk/baumarkt-mcp/commit/74255e7e5f42eacd342fd135e517c07821d0192c))
* **server:** add the FastMCP server with per-retailer degradation ([fbafe80](https://github.com/jnslmk/baumarkt-mcp/commit/fbafe80fad7a37da8e99d980847f4469ee2343a5))


### Bug Fixes

* accept JSON numbers in parse_price ([2bef3e9](https://github.com/jnslmk/baumarkt-mcp/commit/2bef3e9b4fa6bc7f00cd3f2ee569ce9e25dbcfcc))
* **browser:** snapshot pages before waking waiters in release_context ([4f2423f](https://github.com/jnslmk/baumarkt-mcp/commit/4f2423f698da9403b6d125dfe525128f9992820d))
* **obi:** never substitute a different product for an unmatched sku ([4f1af93](https://github.com/jnslmk/baumarkt-mcp/commit/4f1af939f2be90395019c5c8c15ab6d2bd442481))
* refuse percentages, negatives and malformed digit groups in parse_price ([90170a3](https://github.com/jnslmk/baumarkt-mcp/commit/90170a38a61968d89af8961eb44f776b32d0a356))
* **search:** accept limit as an alias for max_results ([682314e](https://github.com/jnslmk/baumarkt-mcp/commit/682314ebf482e30ffb85efd446eb94988375c5ad))


### Documentation

* **globus:** correct stale claims about parse_price ([a9d7335](https://github.com/jnslmk/baumarkt-mcp/commit/a9d733553f1dad48b754457580a0e0a44a152e01))
* **readme:** document the baumarkt-mcp server and its retailers ([aba2e2c](https://github.com/jnslmk/baumarkt-mcp/commit/aba2e2c15e3d81e721e14c278df7485a5ed0b182))
