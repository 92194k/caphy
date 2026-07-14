# Two GitHub accounts, one laptop — push setup

Repo: `https://github.com/92194k/caphy`
- **Account 1** = `92194k` — works in `C:\GitHub\CAPHY` (VS Code). Owns the repo.
- **Account 2** = teammate — works in a **separate folder** (a second clone).

The trick: each folder has its own git identity, and Account 2's folder uses a
Personal Access Token (PAT) in its remote URL so pushes go out as Account 2 (no
credential clash with Account 1).

---

## PHASE 1 — Account 1 pushes the current work (existing folder, VS Code)

In `C:\GitHub\CAPHY` (PowerShell or the VS Code terminal):

```powershell
# set THIS folder's identity to Account 1 (local, not global)
git config user.name  "Account 1 Name"
git config user.email "account1@email.com"     # must be an email on the 92194k account

# stage, commit, push everything
git add .
git commit -m "Rebuild web console, mobile app, phone API"
git push
```

`git push` uses your already-saved 92194k credentials. If it asks, sign in as 92194k.

---

## PHASE 2 — Give Account 2 push access

On github.com, signed in as **92194k**:
1. Open the repo `92194k/caphy` -> **Settings** -> **Collaborators**.
2. **Add people** -> type Account 2's GitHub username -> send invite.
3. Sign into Account 2 (or check its email) and **accept** the invitation.

---

## PHASE 3 — Set up Account 2's folder (one-time)

1. **Make a PAT for Account 2.** Signed in as Account 2:
   GitHub -> **Settings** -> **Developer settings** -> **Personal access tokens** ->
   **Tokens (classic)** -> **Generate new token (classic)** -> tick the **`repo`**
   scope -> Generate -> **copy the token** (shown once).

2. **Clone into a second folder** using that token (PowerShell):

```powershell
cd C:\GitHub
git clone https://ACCOUNT2_USERNAME:ACCOUNT2_TOKEN@github.com/92194k/caphy.git caphy-account2
```

3. **Set Account 2's identity in that folder:**

```powershell
cd caphy-account2
git config user.name  "Account 2 Name"
git config user.email "account2@email.com"     # must be an email on Account 2
```

Now `C:\GitHub\caphy-account2` pushes as Account 2.

---

## PHASE 4 — Everyday use

- **Account 1** works in `C:\GitHub\CAPHY` (VS Code). To save work:
  ```powershell
  git pull        # get teammate's latest first
  git add .
  git commit -m "what I changed"
  git push
  ```
- **Account 2** works in `C:\GitHub\caphy-account2` (same three commands).
- Always `git pull` before you start, and again before you push, to avoid conflicts.

---

## Notes
- The PAT sits in `caphy-account2/.git/config` in plain text. Fine for a school
  project; don't share that folder. To revoke, delete the token on GitHub.
- Commits show under each account only if `user.email` matches an email registered
  on that account (Settings -> Emails). You can use the private
  `NNNNN+username@users.noreply.github.com` address shown there.
- Secrets (`firebase_key.json`, `google-services.json`) are git-ignored — each
  laptop/folder keeps its own copy; they are never pushed.
