import os
import shutil
import subprocess
import logging
import time
import tempfile
import stat
import sqlite3

import config

logger = logging.getLogger(__name__)


def copy_encrypted_db(retry_count=None, retry_delay=None):
    """Copy encrypted database files to a temp directory to avoid lock conflicts."""
    if retry_count is None:
        retry_count = config.COPY_RETRY_COUNT
    if retry_delay is None:
        retry_delay = config.COPY_RETRY_DELAY

    temp_dir = tempfile.mkdtemp(prefix="dingtalk_encrypt_", dir=config.DECRYPTED_DIR)
    dest_db = os.path.join(temp_dir, "dingtalk_encrypted.db")

    # Files to copy: main db + wal + shm
    files_to_copy = [
        (config.ENCRYPTED_DB, dest_db),
        (config.ENCRYPTED_DB + "-wal", dest_db + "-wal"),
        (config.ENCRYPTED_DB + "-shm", dest_db + "-shm"),
    ]

    for attempt in range(retry_count):
        try:
            for src, dst in files_to_copy:
                if os.path.exists(src):
                    shutil.copy2(src, dst)
            logger.info(f"Successfully copied encrypted database to {temp_dir}")
            return dest_db
        except (PermissionError, OSError) as e:
            logger.warning(f"Copy attempt {attempt + 1}/{retry_count} failed: {e}")
            if attempt < retry_count - 1:
                time.sleep(retry_delay)
            else:
                # Clean up partial copy
                shutil.rmtree(temp_dir, ignore_errors=True)
                raise RuntimeError(
                    f"Failed to copy encrypted database after {retry_count} attempts: {e}"
                )

    shutil.rmtree(temp_dir, ignore_errors=True)
    raise RuntimeError("Failed to copy encrypted database")


def decrypt_database(encrypted_db_path=None, output_path=None):
    """Decrypt the database using dingwave CLI tool."""
    # Validate dingwave binary exists
    if not os.path.isfile(config.DINGWAVE_PATH):
        raise FileNotFoundError(
            f"dingwave binary not found at: {config.DINGWAVE_PATH}\n"
            f"Please download it from https://github.com/p1g3/dingwave/releases\n"
            f"and place it in the tools/ directory.\n"
            f"Expected: tools/dingwave.exe (Windows) or tools/dingwave (Linux/Mac)"
        )

    if encrypted_db_path is None:
        encrypted_db_path = copy_encrypted_db()
    if output_path is None:
        output_path = config.DECRYPTED_DB_PATH

    runnable_dingwave = _get_runnable_dingwave_path()

    logger.info(f"Starting decryption: {encrypted_db_path} -> {output_path}")

    cmd = [
        runnable_dingwave,
        "-d", encrypted_db_path,
        "-k", config.USER_UID,
        "-o", output_path,
    ]

    try:
        # dingwave decrypts the DB then starts a web server on port 8080 and never exits.
        # So we use Popen, monitor for the output file, then kill the process.
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        # Wait until decrypted DB is actually readable (not just created as a tiny stub file).
        max_wait = 300  # 5 minutes
        check_interval = 1
        waited = 0
        while waited < max_wait:
            if _is_decrypted_db_ready(output_path):
                break

            # Fail fast if process already exited before DB becomes valid.
            if proc.poll() is not None and not _is_decrypted_db_ready(output_path):
                raise RuntimeError(
                    f"dingwave exited early (code={proc.returncode}) before DB became readable"
                )

            time.sleep(check_interval)
            waited += check_interval

        if waited >= max_wait and not _is_decrypted_db_ready(output_path):
            raise RuntimeError("Timed out waiting for decrypted DB to become readable")

        # Kill the dingwave process (it's blocking on its web server)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            logger.info("dingwave process terminated after decryption")

        # Log any output
        if proc.stdout:
            output = proc.stdout.read()
            for line in output.strip().split("\n"):
                if line.strip():
                    logger.info(f"dingwave: {line.strip()}")

        if not os.path.exists(output_path):
            raise RuntimeError(f"Decryption failed: output file not created at {output_path}")

        output_size = os.path.getsize(output_path)
        logger.info(f"Decryption complete. Output size: {output_size / 1024 / 1024:.1f} MB")

        return output_path

    except Exception:
        # Make sure process is dead on error
        if 'proc' in locals() and proc.poll() is None:
            proc.kill()
            proc.wait()
        raise
    finally:
        # Clean up the encrypted copy
        if encrypted_db_path and os.path.dirname(encrypted_db_path) != config.ENCRYPTED_DB_DIR:
            temp_dir = os.path.dirname(encrypted_db_path)
            shutil.rmtree(temp_dir, ignore_errors=True)
            logger.info(f"Cleaned up temp directory: {temp_dir}")


def _get_runnable_dingwave_path():
    """Return a runnable dingwave binary path.

    On macOS, downloaded binaries can still fail with PermissionError even when chmod +x
    is set. In that case we create a cleaned runtime copy and run that instead.
    """
    binary_path = config.DINGWAVE_PATH
    if not os.path.isfile(binary_path):
        return binary_path

    # Best effort: ensure execute bit exists.
    mode = os.stat(binary_path).st_mode
    if not (mode & stat.S_IXUSR):
        os.chmod(binary_path, mode | stat.S_IXUSR)

    if _can_execute(binary_path):
        return binary_path

    runtime_name = f"{os.path.basename(binary_path)}.runtime"
    runtime_path = os.path.join(config.DECRYPTED_DIR, runtime_name)
    shutil.copy2(binary_path, runtime_path)
    os.chmod(runtime_path, 0o755)

    # Best effort: strip xattrs on macOS so Gatekeeper/provenance does not block launch.
    if os.name == "posix":
        subprocess.run(["xattr", "-c", runtime_path], check=False, capture_output=True)

    if _can_execute(runtime_path):
        logger.warning(
            "dingwave binary is not directly executable; using runtime copy: %s",
            runtime_path,
        )
        return runtime_path

    raise RuntimeError(
        "dingwave binary exists but cannot be executed. "
        f"Checked: {binary_path} and runtime copy: {runtime_path}. "
        "Please verify local security policy or replace the binary."
    )


def _can_execute(path):
    """Quick launch check to verify the binary can start."""
    try:
        subprocess.run(
            [path, "--help"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
        return True
    except (PermissionError, OSError, subprocess.SubprocessError):
        return False


def _is_decrypted_db_ready(path):
    """Return True only when DB exists, is non-empty, and expected tables are queryable."""
    if not os.path.exists(path):
        return False
    if os.path.getsize(path) <= 0:
        return False

    try:
        conn = sqlite3.connect(path)
        try:
            # Parser depends on tbconversation. Use it as readiness signal.
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tbconversation'"
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def sync_decrypt():
    """Full sync: copy encrypted DB, decrypt, return path to decrypted DB."""
    logger.info("=== Starting sync decrypt ===")
    encrypted_copy = copy_encrypted_db()
    decrypted_path = decrypt_database(encrypted_copy)
    logger.info("=== Sync decrypt complete ===")
    return decrypted_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    path = sync_decrypt()
    print(f"Decrypted database: {path}")
