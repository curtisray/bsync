# Distribution and releases

bsync targets macOS and POSIX Linux. The distribution is named **better-rsync**
because [bsync on PyPI](https://pypi.org/project/bsync/) is an unrelated project.
The Python import and installed command remain `bsync`.

## Channels

| Channel | Install | Availability |
| --- | --- | --- |
| Homebrew tap | `brew install curtisray/tap/bsync` | Published in `curtisray/homebrew-tap` |
| pipx via GitHub | `pipx install 'git+https://github.com/curtisray/bsync.git@v0.2.1'` | After the v0.2.1 tag is published |
| uv via GitHub | `uv tool install 'git+https://github.com/curtisray/bsync.git@v0.2.1'` | After the v0.2.1 tag is published |
| pipx via PyPI | `pipx install better-rsync` | Requires the PyPI activation below |
| uv via PyPI | `uv tool install better-rsync` | Requires the same PyPI activation |
| pip via PyPI | `pip install better-rsync` inside a venv | Requires the same PyPI activation |
| apt/dnf/pacman, Nix, Conda | Separate downstream recipes | Not yet published |
| Winget/Scoop/Chocolatey | Requires a native Windows port first | Unsupported; use WSL today |

PyPI is the registry; pip, pipx, and uv are alternative installers using that
registry. They do not need separate package submissions. Python installers cannot
install the system rsync executable. Homebrew can declare both Python and rsync
as dependencies and is therefore the simplest complete install.

## First-time PyPI activation (account owner)

1. Create or sign into [PyPI](https://pypi.org/account/register/) and configure
   its required two-factor authentication.
2. Open [Publishing](https://pypi.org/manage/account/publishing/) and add a
   **pending GitHub publisher** using these exact values:

   | Field | Value |
   | --- | --- |
   | PyPI project name | `better-rsync` |
   | GitHub owner | `curtisray` |
   | Repository | `bsync` |
   | Workflow filename | `publish-pypi.yml` |
   | Environment | `pypi` |

3. Create the GitHub environment named `pypi` in the repository's settings
   if it does not exist. Restrict it to trusted maintainers as appropriate.
4. In GitHub Actions, run **Publish to PyPI** from `main`, with `tag: v0.2.1`
   (or the existing GitHub release to publish).
5. Confirm the new release appears at
   `https://pypi.org/project/better-rsync/`, then update the README's PyPI
   availability language and test a fresh `pipx install better-rsync`.

The workflow downloads the existing release wheel and sdist, checks their metadata
and SHA-256 checksums, and publishes the same artifacts using PyPI Trusted
Publishing. No long-lived PyPI API key is needed. Registering a pending publisher
does not reserve the name; availability must still be checked before first upload.

## Releasing a new version

1. Change `__version__` in `bsync.py`. Package metadata reads this value directly.
2. Run the tests and review the diff. Push the version commit to `main`.
3. Create and push an annotated stable tag, for example:

   ```sh
   git tag -a v0.2.2 -m 'Release bsync 0.2.2'
   git push origin v0.2.2
   ```

4. The **Release** workflow runs the macOS/Linux and Python 3.10/3.14 test matrix,
   builds a source distribution and wheel, checks package metadata with Twine,
   and publishes a GitHub release containing:
   - `better_rsync-<version>.tar.gz`
   - `better_rsync-<version>-py3-none-any.whl`
   - `SHA256SUMS` (checksums of both distributions)
   - `bsync.rb` (the generated Homebrew formula)
5. Copy the release's `bsync.rb` into `Formula/bsync.rb` in
   `curtisray/homebrew-tap`, test it with Homebrew, then commit and push the tap.
6. Once PyPI is activated, run **Publish to PyPI** with the same release tag.

Do not move published tags or replace released distributions. Version bumps
produce new immutable URLs and new checksums. Homebrew tap updates are explicit;
the source repository's Actions token does not have cross-repository write access.

## Local release validation

Use a fresh output directory so old versions are not accidentally released:

```sh
python -m pip install build twine
python -m build --outdir /tmp/bsync-release
python -m twine check /tmp/bsync-release/*
python scripts/prepare_release.py --tag v0.2.1 --dist /tmp/bsync-release
python scripts/prepare_release.py --tag v0.2.1 --dist /tmp/bsync-release --verify
```

`scripts/homebrew-resources.json` pins versions, universal wheel URLs, and SHA-256
hashes for all Homebrew Python resources, including the build backend. The formula
installs them without dependency resolution or isolated build downloads. Update
and test this file when dependencies change. This tap uses universal Python wheels
for its resources; a future Homebrew core submission may require source recipes
and must satisfy Homebrew's current acceptance policy.

## Additional managers

Homebrew core is a separate reviewed submission; maintaining a working tap does
not automatically make `brew install bsync` available. Until accepted, use the
fully qualified `brew install curtisray/tap/bsync`.

For Linux, the next useful additions are an AUR PKGBUILD, a Nix package, and a
Conda-forge recipe. Debian/Ubuntu and Fedora support require maintained packages
or signed repositories. Each needs its own dependency declarations, updates,
tests, and upstream review where applicable. The existing source distributions
and versioned releases provide the inputs for those recipes.

Native Windows packaging requires replacing `fcntl`, `termios`, POSIX selectors,
and process-group signals and providing a compatible rsync runtime. Publishing a
Windows installer before that work would produce an installer that cannot run.

References: [Homebrew taps](https://docs.brew.sh/How-to-Create-and-Maintain-a-Tap),
[Python formulae](https://docs.brew.sh/Language-Specific-Formulae#python),
[PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/),
[uv tools](https://docs.astral.sh/uv/guides/tools/),
[pipx](https://pipx.pypa.io/stable/).
