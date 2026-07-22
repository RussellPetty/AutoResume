# Contributing

Thanks for helping improve AutoResume.

1. Open an issue before large behavior or provider-support changes.
2. Keep the runtime Python 3.9+ standard-library-only. tmux is the sole runtime dependency.
3. Preserve the fail-closed security model: new detection must not approve tools, bypass permissions, read credentials, overwrite drafts, or retry non-subscription failures.
4. Add unit fixtures for parser/protocol changes and tmux integration coverage for injection behavior.
5. Run the full verification suite before submitting a pull request:

```bash
python3 -m py_compile src/autoresume.py src/statusline.py
python3 -m unittest discover -s tests -v
bash -n install.sh
```

Use independently generated fixtures or public documented behavior. Do not contribute leaked, reverse-engineered, or non-redistributable provider code or data.

By contributing, you agree that your contribution is licensed under the MIT License.
