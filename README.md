# Morphe Auto-Patcher

A GitHub Actions project for building Morphe-patched Android apps, with a web manager hosted through GitHub Pages.

## Start here

1. **Fork this repository** to your GitHub account.
2. **Enable GitHub Actions and Pages** in your fork. Publish the `main` branch's `/docs` folder.
3. **Open your fork's web manager** and connect it with a fine-grained token for that fork.
4. **Add your patch source and apps** in the manager, then start a build.

The detailed setup steps are below. Use your own fork so your settings, builds, and APK releases stay in your repository.

## Fork and set up your copy

Create a fork first. Your fork stores your app list, patch sources, uploaded original APKs, build history, and published APKs. Do not connect the manager to the upstream project unless you own it.

### 1. Fork this repository

On GitHub, open [Rashedul0103/morphe-auto-patcher](https://github.com/Rashedul0103/morphe-auto-patcher) and choose **Fork**. Keep the repository name `morphe-auto-patcher` so the links below work as written.

### 2. Enable GitHub Actions

In your fork, open **Settings → Actions → General** and make sure Actions are allowed. The workflow runs checks before builds and needs permission to write generated catalog updates to your fork.

### 3. Turn on the web manager

Open **Settings → Pages**. Under **Build and deployment**, choose **Deploy from a branch**, then select branch **main** and folder **/docs**, and save.

After GitHub publishes the site, open:

`https://YOUR-GITHUB-USERNAME.github.io/morphe-auto-patcher/`

Replace `YOUR-GITHUB-USERNAME` with your GitHub username.

### 4. Create a token for the manager

Create a **fine-grained personal access token** in GitHub settings. Limit it to your fork and grant these repository permissions:

- **Contents: Read and write**
- **Actions: Read and write**

Keep the token private. The manager saves it in your browser so it can update your fork and start builds. Never add it to `config.json`, a commit, an issue, or a pull request.

### 5. Connect your fork

In the web manager, connect using your GitHub username, repository name `morphe-auto-patcher`, branch `main`, and the token you just created.

### 6. Add a patch source and app

In **Patch sources**, add the patch repository you want to use. Then add an app from that source and choose the patches and options you want.

This project starts with no apps or patch sources configured. Each fork owner adds their own.

### 7. Build and download

Open the app, choose **Patch / Rebuild**, and start the build. GitHub Actions runs it in your fork; successful APKs appear under **Releases** and in the manager’s **Patched APKs** page.

The build tries to obtain the original app automatically. If it cannot, download an untouched original APK for the requested version and upload it through the manager, then retry the build.

### 8. Optional Discord alerts

To receive build alerts, add a repository secret named `DISCORD_WEBHOOK` in **Settings → Secrets and variables → Actions**. Do not commit the webhook URL.

## Contributing changes

Make changes in your fork and open a pull request to this repository. Keep personal app configuration, tokens, webhook URLs, and APK files out of pull requests.

## Help

If a step fails, check the **Actions** tab in your fork for the build log. Include the failing step name and relevant error text when opening an issue; remove tokens, webhook URLs, and other secrets first.
