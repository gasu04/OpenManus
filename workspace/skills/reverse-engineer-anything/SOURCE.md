# Source and version

- Upstream project: [morluto/rea](https://github.com/morluto/rea)
- Package: `rea-agents` **6.8.0** from the npm registry
- Bundled skill version: **49** (`metadata.tool_count: 229`)
- Source of these files: `third_party/rea/node_modules/rea-agents/skills/reverse-engineer-anything/`
- Vendored files: `SKILL.md`, all seven files under `references/`, and `LICENSE`
- `OPENMANUS.md` and this `SOURCE.md` are OpenManus-specific additions. The
  upstream skill and reference documents are unmodified.

The local npm installation is intentionally excluded from git. Recreate it with
Node.js 22 (minimum supported by this REA release):

```bash
PATH="$HOME/.nvm/versions/node/v22.23.4/bin:$PATH" \
  npm install --prefix third_party/rea --no-audit --no-fund rea-agents@6.8.0
```

To refresh the vendored skill deliberately, install and test a specific newer
REA version first, then copy that package's bundled skill and references here.
Do not copy from GitHub main: its instructions may describe tools that the
registered server does not provide.
