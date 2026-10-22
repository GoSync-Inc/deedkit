"""Keep the anymsg distributions self-contained while tests retain their paths."""

from pathlib import Path
import tempfile

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


ASSETS = (
    "bounds.mjs",
    "capture.mjs",
    "history.mjs",
    "identity.mjs",
    "ipc.mjs",
    "media.mjs",
    "navigation.mjs",
    "pair.mjs",
    "pairing.mjs",
    "provider.mjs",
    "sender.mjs",
    "outgoing.mjs",
    "transport-state.mjs",
    "read-state.mjs",
    "receipts.mjs",
    "operations.mjs",
    "package.json",
    "package-lock.json",
)

# Reviewed source inputs only. Never include the native runtime's auth, journal,
# media cache, pairing QR or a platform-specific executable in a distribution.
NATIVE_SOURCES = (
    "go.mod", "go.sum", "main.go", "config.go", "runtime.go", "capture.go",
    "history.go", "store.go", "send.go", "media.go", "operations.go", "group_iq.go",
    "runtime_test.go", "send_test.go", "media_test.go", "operations_test.go",
    "fork.patch",
)


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        source = Path(self.root)
        # An unpacked sdist contains its assets; a checkout has the unchanged
        # collector subtree at repository root. Never scan runtime directories.
        repository = source if (source / "whatsapp").is_dir() else source.parents[1]
        force = build_data["force_include"]
        for name in ASSETS:
            path = repository / "whatsapp" / name
            if not path.is_file() or path.is_symlink():
                raise ValueError("missing reviewed WhatsApp build asset")
            target = (
                "messaging_service/whatsapp_assets"
                if self.target_name == "wheel"
                else "whatsapp"
            )
            force[str(path)] = target + "/" + name
        if self.target_name == "sdist":
            for name in NATIVE_SOURCES:
                path = repository / "whatsapp" / "whatsmeow" / name
                if not path.is_file() or path.is_symlink():
                    raise ValueError("missing reviewed native WhatsApp build asset")
                force[str(path)] = "whatsapp/whatsmeow/" + name
            for name in (
                "SETUP.md",
                "media_benchmark.py",
                "client_check.py",
                "licenses/age-LICENSE",
            ):
                force[str(repository / name)] = name
            # Docker's source archive layout remains the published anymsg layout.
            self._temporary = tempfile.TemporaryDirectory(prefix="anymsg-build-")
            docker = Path(self._temporary.name) / "Dockerfile"
            docker.write_text(
                (source / "Dockerfile").read_text().replace("services/messaging/", "")
            )
            force[str(docker)] = "Dockerfile"

    def finalize(self, version, build_data, artifact_path):
        if hasattr(self, "_temporary"):
            self._temporary.cleanup()
