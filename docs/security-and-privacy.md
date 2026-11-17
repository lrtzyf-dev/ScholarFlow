# Security and privacy

- Keep API keys in local environment variables or the application settings; never commit `.env` files, credentials, databases, or real research documents.
- The repository ignores `.env` files except `.env.example`, local data directories, SQLite files, Python environments, and frontend build/dependency outputs.
- Configure a cloud provider only if you intend to send prompts or selected source text to that provider; review its data-retention and privacy terms.
- In development mode the backend stores data under `backend/data` by default. Use an explicit disposable `DATA_DIR` when testing.
- Windows credential protection is tied to the current Windows user. Do not copy encrypted application credentials between user accounts and expect them to work.
- Project commands and terminal sessions run with the permissions of the current OS user. They are not isolated by an OS-level sandbox.
- The included PDF is synthetic demonstration material. It contains no real paper or user data.

If you discover a security issue, do not post credentials, private papers, or database files in a public issue. Use GitHub's private vulnerability reporting for the repository when enabled.
