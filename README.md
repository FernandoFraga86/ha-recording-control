# Home Assistant Recording Control — experimental distribution candidate

**Status: EXPERIMENTAL — NOT INSTALLABLE — IMAGE BUILD/PUBLISH NOT YET VERIFIED.**

This repository contains a source patch for an unofficial Home Assistant app that integrates with Frigate, plus Supervisor metadata templates. It is for review only. It is not an official Frigate or Home Assistant project and is not ready for installation or production use.

## Frozen source identity

- Upstream base: Frigate 0.18.2, commit `cfa08c56b23d23280c3d0a218e5a43182c97445b`.
- Candidate source snapshot: `1ca90f845603cd30e1dd241fd9e3ce6506a36841`.
- Source overlay SHA-256: `acc70d496dc667a2b44241b4bf527d6d8e8fe08a2e47d32ff4667598d45ff6be`.
- Published patch file SHA-256: `512f404a274aeb11a3e26246cddae48a62539cc01ccb10d6a297c5ea42221b65`.
- Distribution branding patch SHA-256: `35dddbc08947abb8db1b97e7faeb4855d323f8f0fa722db9756afcf20a2e0a6a`.
- Current candidate app/image version string: `0.18.2-recording-control-1ca90f8-rebrand1-rootfs1`.
- Planned image reference: `ghcr.io/fernandofraga86/ha-recording-control:0.18.2-recording-control-1ca90f8-rebrand1-rootfs1`.

The repository contains source patches against the pinned upstream base, verification and image-repacking tools, the manual GitHub Actions build workflow, and `.template` Supervisor files. It does not include the full upstream source tree, a locally built image, household configuration, camera addresses or credentials, recordings, backups, or host/VM identifiers.

For privacy, one synthetic username in a test fixture was generalized from a household-identifiable name to `test_viewer`. No runtime source hunk was changed by this publication-only sanitization; the overlay digest above identifies the original frozen build snapshot, while the patch hash identifies this sanitized public patch.

The app template intentionally retains the candidate's Supervisor API, Home Assistant API, MQTT, media, app-configuration, and Home Assistant configuration mappings. Their final necessity and privacy impact have not been revalidated for this public candidate; do not install the template.

## Distribution and validation status

The visual distribution delta removes 12 upstream Frigate logo/icon/favicon assets and replaces the app-facing identity with original `HA Recording Control` artwork. The server/runtime overlay is constrained by exact SHA-256 checks in `candidate-verification.json`; the branding patch does not change the security/runtime source files. The direct-from-source image build stopped at the unavailable Kaltura `nginx-secure-token-module` 1.5 archive in run `42403520281`. The current `rootfs1` strategy instead starts from the exact official Frigate 0.18.2 OCI image: tag index `sha256:fd60f99ffd4eb6c42360ed4e14953e270586f239309ab3983e55b2e97ffac09a`, `linux/amd64` manifest `sha256:9ebab4801be69ab4ebc7f6ab1d582c859ff7412d457a3fa550056894e8444dad`, and config `sha256:d8c79fd30635c830c5972dbc61c8c6c789af2373c654e7f4de1073eef435c982`. The workflow verifies the official tag-to-manifest relationship, compares the image's Python source tree with the pinned upstream commit, replaces the candidate server and compiled UI, then exports and imports a flattened root filesystem so protected logos do not remain in lower OCI layers. It reconstructs and checks the supported runtime configuration and refuses unexpected volumes or unsupported metadata. The secure-token Nginx module is inherited from the pinned official image and must pass compile-flag, active-config parsing, and synthetic manifest behavior checks before publication. Upstream copyright, MIT license, trademark policy, and brand-asset attribution text are retained in the image.

The first remote workflow attempt (`42390470070`) stopped before building the image: the pinned upstream Vitest configuration points to `web/__test__/test-setup.ts`, which does not exist in the pinned commit, so Vitest executed zero tests. This is recorded as unavailable, not as a passing test result. The second attempt (`42391502002`) passed lint and the production UI build but failed Playwright because production assets used `/BASE_PATH/` while the preview server served at `/`. The third attempt (`42396677158`) corrected the E2E base path and passed 307 tests with 85 skipped; the next attempt (`42397657547`) reproduced that E2E result and confirmed that Vite does not place the source `site.webmanifest` in its production output. The distribution Dockerfile now copies that manifest into the final web root, and the isolated image smoke test requests it over HTTP as well as `/api/version`. Attempt `42398678376` stopped before E2E because the workflow used a repository-relative path from inside `web/`; that path was corrected. Attempt `42398918610` then passed E2E and both compiled-UI checks, but the workflow used an incorrect Dockerfile path; this was corrected and attempt `42399532334` reached the image build after passing E2E (307 passed, 85 skipped) and both compiled-UI checks. Attempts `42401468371`, `42402548585`, and `42403520281` reached progressively later image-build stages; the last verified the VOD archive but then stopped at the missing Kaltura secure-token source. The new `rootfs1` workflow ran remotely as `42408071455`: source, UI, lint and Playwright checks passed, but image assembly first stopped on an upstream-commit field read from the wrong lock schema. That lookup was corrected; run `42409096853` then passed source/UI/Playwright gates but stopped before publish on a provenance-label lookup that did not match `candidate-verification.json`. Run `42410063966` passed source/UI/Playwright gates and verified the digest-pinned base, then stopped before publish because Docker import cannot encode the legacy `ArgsEscaped` field. The local correction now accepts its omission only for the verified Linux image, retaining the exact command arrays; a non-Linux base remains rejected. This change is covered by local tests and still requires a new remote run. No image was published and no OCI digest is claimed. Publication remains conditional on candidate source checks, exact base-image and runtime-config verification, brand-layer scan, Nginx module behavior, packaged API tests, isolated service smoke, a unique unused tag, and anonymous GHCR manifest verification. The target metadata records Supervisor 2026.10.5 and `amd64` as intended review targets only; loading, installation, recovery, privacy, and startup behavior have not been validated against that Supervisor. Historical test summaries are not represented as a run bound to this rebranded distribution.

The app templates deliberately retain the `.template` suffix. This repository has no root `repository.yaml`, and no app file is named `config.yaml`; Home Assistant therefore cannot discover this source-only snapshot as an installable app repository. Do not rename the templates or install this candidate.

The version tag is unique to this rebrand variant. The registry tag must be checked as unused immediately before publication, and the OCI digest recorded after publication. Registry-level immutable-tag policy is not assumed. The app remains non-installable until a separate review validates the complete app with the target Supervisor.

The app template remains `config.yaml.template`; no root `repository.yaml` or discoverable `config.yaml` is added. `production_ready=false`, `installable=false`, and `native_recovery_enabled=false` remain explicit.

## Trademark and attribution

The upstream source is MIT-licensed; retain its copyright and license notice. Frigate names and visual brand assets are not licensed by MIT. “Frigate” is used here only to identify compatibility. No Frigate logo is included. A future distributed runnable derivative requires a separate branding review and must not imply endorsement by Frigate, Inc. or the Home Assistant project.

See `LICENSE`, `UPSTREAM-TRADEMARK-POLICY.md`, `SOURCE-PROVENANCE.json`, and `distribution-lock.json`.

The workflow can only be run manually from the repository's default branch. It uses a public GitHub-hosted runner and never contacts the household HA, MQTT broker, Supervisor, or camera devices.
