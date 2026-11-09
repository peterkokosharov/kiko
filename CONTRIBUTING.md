# Contributing to kiko

kiko is a personal project. Contributions are welcome as bug reports and
pull requests. By submitting a pull request you agree that your contribution
will be licensed under the MIT License.

## Development

Install in a dedicated virtual environment:

    python -m venv .venv
    source .venv/bin/activate   # Windows: .venv\Scripts\activate
    pip install -e ".[dev]"
    pytest tests/kiko

## Code style

Follow existing conventions in the code. No external formatters are required.

## Reporting issues

Open a GitHub issue. For security issues, see SECURITY.md.
