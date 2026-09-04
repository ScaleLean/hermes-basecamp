# Contributing

Thank you for helping improve Hermes Basecamp.

## Development setup

1. Install Python 3.11, 3.12, or 3.13.
2. Install a local checkout of [Hermes Agent](https://github.com/NousResearch/hermes-agent).
3. Create a virtual environment that can import Hermes.
4. Install this project with its development dependencies:

   ```sh
   python -m pip install -e '.[dev]'
   ```

5. Run the checks:

   ```sh
   python -m pytest -q
   ruff check .
   hermes plugins doctor . --ci
   ```

## Pull requests

- Keep each change focused.
- Add a regression test for behavior changes.
- Preserve the identity lock and project allowlist.
- Never commit credentials, OAuth tokens, Basecamp exports, or customer data.
- Use synthetic fixtures. Do not record live Basecamp responses.
- Describe external API assumptions in the pull request.

By contributing, you agree that your contribution is licensed under the MIT
License.
