# Publish kiko-lite to your own GitHub (for a human and an AI agent)

**Author:** Peter Kokosharov

## Part A. For the human (about 5 minutes)

1. On github.com (in the browser) create a new EMPTY PRIVATE repository, for example `kiko-lite`.
   Do not add a README, .gitignore or license.
2. Create a fine-grained token: Settings > Developer settings > Personal access tokens > Fine-grained tokens.
   Repository access: "Only select repositories", choose ONLY that one repository.
   Permissions: Contents = Read and write. Expiration: choose "Custom" and pick tomorrow's date (there is no "1 day" preset).
3. Give the AI agent: the kiko-lite zip, the token, your GitHub user name and the repository name.
4. Warning: anything you give to a cloud AI is sent to that company's servers. Give it only this short-lived,
   single-repository token. Never a classic token, never your password.
5. When the agent reports success, revoke the token at once: Settings > Developer settings > Personal access
   tokens > Fine-grained tokens > the token > Delete.

## Part B. For the AI agent (follow exactly, in a Linux/macOS shell)

On Windows use Git Bash. Needs git 2.31 or newer.
Rules: never print the token, never put it in a URL or a file, never use `git remote add` with it, never
force-push. Replace `<user>`, `<repo>` and `<version>` with the real values.

1. Keep the token in memory only (do not echo it). The first line stops the shell from saving the token in
   its history file:

        set +o history; unset HISTFILE
        export GH_TOKEN='<token>'

2. Build the header once; it must not be empty:

        AUTH=$(printf 'x-access-token:%s' "$GH_TOKEN" | base64 | tr -d '\n')
        [ -n "$AUTH" ] || echo "ERROR: empty header, check GH_TOKEN"

3. Unpack into a new empty folder:

        mkdir kiko-lite-publish && cd kiko-lite-publish
        unzip ../kiko-lite-<version>.zip

4. Check for secrets. This must print nothing; if it prints anything, STOP and tell the human:

        find . \( -name '.env*' -o -name '*.db' -o -name '*.sqlite' -o -name '*.pem' -o -name '*.key' \
        -o -name '*.p12' -o -name '*.pfx' -o -name 'id_rsa*' -o -name 'id_ed25519*' -o -name '.netrc' \
        -o -name 'credentials*.json' \) -print

5. Create the repository and commit (the author is fixed):

        git init -b main      # git older than 2.28: git init && git checkout -b main
        git add -A
        git -c user.name="Peter Kokosharov" -c user.email="peter.kokosharov@gmail.com" commit -m "kiko-lite: first import"

6. Push. The token goes in a one-time header, so it is not stored in `.git/config` or in the URL.
   `GIT_TERMINAL_PROMPT=0` and `-c credential.helper=` stop prompts and saving credentials (also in Git
   Credential Manager on Windows):

        GIT_TERMINAL_PROMPT=0 GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader \
        GIT_CONFIG_VALUE_0="Authorization: Basic $AUTH" \
        git -c credential.helper= push https://github.com/<user>/<repo>.git main

7. Verify. The sha printed for `refs/heads/main` must equal the local one:

        GIT_TERMINAL_PROMPT=0 GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader \
        GIT_CONFIG_VALUE_0="Authorization: Basic $AUTH" \
        git -c credential.helper= ls-remote https://github.com/<user>/<repo>.git
        git rev-parse HEAD

8. Clean up: run `unset GH_TOKEN AUTH`.
9. Report to the human: the repository URL `https://github.com/<user>/<repo>`, the commit sha, and this line:
   "Please revoke the token now (Settings > Developer settings > Personal access tokens > Fine-grained tokens > (token) > Delete)."

## Troubleshooting

* **401**: the token is wrong or expired. The human must make a new token.
* **403**: the token has the wrong permission or is not for this repository. The human must create a new
  fine-grained token for exactly this repository with Contents = Read and write.
* **404** (repository not found): the user or repository name is wrong, or the token cannot see this private
  repository. Check the spelling with the human.
* **non-fast-forward / rejected**: the repository is not empty. Do NOT force-push and do NOT pull. Stop and ask
  the human.
