
# Database connection and query helpers
# Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering 
# MIT License
#
# Storage layout:
#   objects/
#     a1/
#       b2c3d4e5f6...  (full file content, optionally gzipped)
#
# Objects are named by SHA-256 hash of their content.
# First 2 hex chars = subdirectory, rest = filename.
# This avoids filesystem issues with too many files in one directory.

import gzip
import hashlib
import os
from pathlib import Path


DEFAULT_OBJECTS_DIR = "objects"


def hash_content(content: bytes) -> str:
    """SHA-256 hash of raw bytes, returns 64-char hex string."""
    return hashlib.sha256(content).hexdigest()


def hash_file(filepath: str) -> str:
    """SHA-256 hash of a file's content."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _obj_path(objects_dir: str, obj_hash: str) -> str:
    """Convert hash to filesystem path: objects/a1/b2c3d4..."""
    return os.path.join(objects_dir, obj_hash[:2], obj_hash[2:])


def store_blob(content: bytes, objects_dir: str = DEFAULT_OBJECTS_DIR,
               compress: bool = False) -> str:
    """
    Store content in the object store. Returns the hash.
    If the object already exists, does nothing (content-addressable = idempotent).
    """
    obj_hash = hash_content(content)
    path = _obj_path(objects_dir, obj_hash)

    if os.path.exists(path):
        return obj_hash  # already stored

    os.makedirs(os.path.dirname(path), exist_ok=True)

    if compress:
        with gzip.open(path, "wb") as f:
            f.write(content)
    else:
        with open(path, "wb") as f:
            f.write(content)

    return obj_hash


def store_file(filepath: str, objects_dir: str = DEFAULT_OBJECTS_DIR,
               compress: bool = False) -> str:
    """Store a file's content in the object store. Returns the hash."""
    with open(filepath, "rb") as f:
        content = f.read()
    return store_blob(content, objects_dir, compress)


def retrieve_blob(obj_hash: str, objects_dir: str = DEFAULT_OBJECTS_DIR) -> bytes | None:
    """Retrieve content by hash. Returns bytes or None if not found."""
    path = _obj_path(objects_dir, obj_hash)

    if not os.path.exists(path):
        return None

    # Try gzip first, fall back to raw
    try:
        with gzip.open(path, "rb") as f:
            return f.read()
    except gzip.BadGzipFile:
        with open(path, "rb") as f:
            return f.read()


def retrieve_to_file(obj_hash: str, dest_path: str,
                     objects_dir: str = DEFAULT_OBJECTS_DIR) -> bool:
    """Retrieve an object and write it to a file. Returns True on success."""
    content = retrieve_blob(obj_hash, objects_dir)
    if content is None:
        return False

    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    with open(dest_path, "wb") as f:
        f.write(content)
    return True


def exists(obj_hash: str, objects_dir: str = DEFAULT_OBJECTS_DIR) -> bool:
    """Check if an object exists in the store."""
    return os.path.exists(_obj_path(objects_dir, obj_hash))


def object_size(obj_hash: str, objects_dir: str = DEFAULT_OBJECTS_DIR) -> int:
    """Get the stored size of an object in bytes. Returns -1 if not found."""
    path = _obj_path(objects_dir, obj_hash)
    if not os.path.exists(path):
        return -1
    return os.path.getsize(path)


def list_objects(objects_dir: str = DEFAULT_OBJECTS_DIR) -> list[str]:
    """List all object hashes in the store."""
    hashes = []
    if not os.path.exists(objects_dir):
        return hashes

    for prefix_dir in sorted(os.listdir(objects_dir)):
        prefix_path = os.path.join(objects_dir, prefix_dir)
        if not os.path.isdir(prefix_path) or len(prefix_dir) != 2:
            continue
        for obj_file in sorted(os.listdir(prefix_path)):
            hashes.append(prefix_dir + obj_file)

    return hashes


def gc_unreferenced(referenced_hashes: set[str],
                    objects_dir: str = DEFAULT_OBJECTS_DIR) -> int:
    """Delete objects not in the referenced set. Returns count deleted."""
    deleted = 0
    for obj_hash in list_objects(objects_dir):
        if obj_hash not in referenced_hashes:
            path = _obj_path(objects_dir, obj_hash)
            os.remove(path)
            deleted += 1

            # Clean up empty prefix directories
            prefix_path = os.path.dirname(path)
            if os.path.isdir(prefix_path) and not os.listdir(prefix_path):
                os.rmdir(prefix_path)

    return deleted
