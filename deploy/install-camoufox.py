"""Install the locked Camoufox release from its checksummed GitHub assets."""

import platform

from camoufox.browser_pin import load_pin
from camoufox.fpgen_model import ensure_fpgen_model
from camoufox.pkgman import AvailableVersion, CamoufoxFetcher, RepoConfig, Version


def main():
    # The CLI uses the anonymous GitHub API even for its paired browser release.
    # Use that same release and installer with fixed asset digests at build time.
    pin = load_pin()
    if pin is None or pin.tag != "v158.0.1-beta.41":
        raise RuntimeError("Update the Camoufox asset digests to match the locked browser release")

    asset_arch = {"x86_64": "x86_64", "aarch64": "arm64"}[platform.machine()]
    digest = {
        "x86_64": "3f2eb1d0451fa58e45e43e55f5e6181565069791ae60c0574f0ceb1f01341c33",
        "arm64": "60e25f6eb95e13d9ec655bc02913d51057e669cb31542e6ace9d2190ab5380af",
    }[asset_arch]
    release_url = f"https://github.com/{pin.repo}/releases/download/{pin.tag}"
    browser = AvailableVersion(
        version=Version(build=pin.build, version=pin.version),
        url=f"{release_url}/camoufox-{pin.spec}-lin.{asset_arch}.zip",
        is_prerelease=True,
        sha256=digest,
    )
    CamoufoxFetcher(RepoConfig.find_by_name(pin.repo_name), selected_version=browser).install()
    ensure_fpgen_model()


if __name__ == "__main__":
    main()
