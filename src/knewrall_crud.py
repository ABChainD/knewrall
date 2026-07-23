"""
Knewrall CRUD Module

Provides deterministic, file‑backed CRUD operations for Knewrall nodes.
All JSON files are saved with sorted keys and sorted arrays to guarantee
byte‑identical output for identical data.
"""

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union
from pathlib import Path

from .paths import get_root

# Schema validation will be added in Phase 2
# from jsonschema import validate, ValidationError

NEURONS_DIR = get_root() / "neurons"

# Neuron files are sharded into sub-folders by the first SHARD_WIDTH hex
# characters of their UUID (e.g. neurons/ab/ab086df7-...json) so that no single
# directory grows unbounded as the graph scales. The shard is a pure function of
# the UUID, so a file's location is always deterministic.
SHARD_WIDTH = 2


def _ensure_neurons_dir() -> None:
    """Create the neurons directory if it doesn't exist."""
    NEURONS_DIR.mkdir(parents=True, exist_ok=True)


def _shard(uuid_str: str) -> str:
    """Return the shard sub-folder name for a given UUID."""
    return uuid_str[:SHARD_WIDTH].lower()


def neuron_json_path(uuid_str: str) -> Path:
    """Return the sharded path to a neuron's .json source file."""
    if uuid_str.endswith(".json"):
        uuid_str = uuid_str[:-5]
    return NEURONS_DIR / _shard(uuid_str) / f"{uuid_str}.json"


def neuron_md_path(uuid_str: str) -> Path:
    """Return the sharded path to a neuron's .md companion file."""
    if uuid_str.endswith(".md"):
        uuid_str = uuid_str[:-3]
    return NEURONS_DIR / _shard(uuid_str) / f"{uuid_str}.md"


def _deterministic_sort(obj: Any) -> Any:
    """
    Recursively sort dictionary keys and arrays to produce deterministic output.

    Rules:
    - Dicts: keys sorted alphabetically.
    - Arrays of primitives (str, int, float, bool): sorted naturally.
    - Arrays of dicts: sorted by a stable key if possible (e.g., 'target_id').
    - Nested structures processed recursively.
    """
    if isinstance(obj, dict):
        # Sort keys alphabetically
        sorted_dict = {k: _deterministic_sort(v) for k, v in sorted(obj.items())}
        return sorted_dict
    elif isinstance(obj, list):
        if not obj:
            return []
        
        # Recursively sort each element first so that we compare sorted structures
        items = [_deterministic_sort(item) for item in obj]

        # Determine if list contains dicts with a common sortable key
        first = items[0]
        if isinstance(first, dict):
            # Try to find a suitable sorting key
            if "target_id" in first:
                # For links, sort by target_id and predicate
                sorted_list = sorted(items, key=lambda x: (x.get("target_id", ""), x.get("predicate", "")))
            elif "value" in first:
                # For properties, sort by value and temporal/spatial markers
                sorted_list = sorted(items, key=lambda x: (str(x.get("value", "")), x.get("when", ""), x.get("where", "")))
            else:
                # Fallback: sort by string representation of the whole dict
                sorted_list = sorted(items, key=lambda x: json.dumps(x, sort_keys=True))
        else:
            # List of primitives – sort naturally
            sorted_list = sorted(items, key=lambda x: (str(type(x)), str(x)))
        
        return sorted_list
    else:
        # Primitive value – return as‑is
        return obj


def _deep_merge(base: Any, updates: Any) -> Any:
    """
    Recursively merge two structures.
    - Dicts: merged by keys.
    - Lists: merged by appending unique elements.
    - Primitives: overwritten.
    """
    if isinstance(base, dict) and isinstance(updates, dict):
        new_dict = base.copy()
        for key, value in updates.items():
            if key in new_dict:
                new_dict[key] = _deep_merge(new_dict[key], value)
            else:
                new_dict[key] = value
        return new_dict
    elif isinstance(base, list) and isinstance(updates, list):
        # Combine lists and ensure uniqueness of elements
        combined = list(base)
        for item in updates:
            if item not in combined:
                combined.append(item)
        return combined
    else:
        # If types differ or it's a primitive/null, overwrite
        return updates


def deterministic_json(data: Dict) -> str:
    """
    Convert a dictionary to a deterministically formatted JSON string.

    Args:
        data: The dictionary to serialize.

    Returns:
        JSON string with sorted keys, sorted arrays, and consistent indentation.
    """
    sorted_data = _deterministic_sort(data)
    return json.dumps(sorted_data, sort_keys=True, indent=2, ensure_ascii=False)


def generate_uuid() -> str:
    """Return a UUIDv4 string."""
    return str(uuid.uuid4())


def now_iso() -> str:
    """Return current UTC time in ISO‑8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save_node(node_data: Dict) -> str:
    """
    Validate, format, and save a node to `neurons/<shard>/[UUID].json`.

    The node_data must already contain a valid `system.id` (UUIDv4).
    This function adds/updates `system.last_updated` and optionally
    `system.checksum`, then writes the file.

    Args:
        node_data: The node dictionary conforming to the master schema.

    Returns:
        The file path where the node was saved.

    Raises:
        ValueError: If the node data is missing required fields.
        IOError: If the file cannot be written.
    """
    _ensure_neurons_dir()

    # Ensure system fields are present
    if "system" not in node_data:
        node_data["system"] = {}
    sys = node_data["system"]
    if "id" not in sys:
        raise ValueError("node_data.system.id is required")
    if "version" not in sys:
        sys["version"] = 1
    sys["last_updated"] = now_iso()

    # Optional checksum (can be added later)
    # if "checksum" not in sys:
    #     sys["checksum"] = compute_checksum(node_data)

    # Validate against schema (placeholder)
    # validate(node_data, MASTER_SCHEMA)

    # Deterministic formatting
    json_str = deterministic_json(node_data)

    # Write file to its sharded location (neurons/<prefix>/<uuid>.json)
    file_path = neuron_json_path(sys["id"])
    file_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(json_str)
    except OSError as e:
        raise IOError(f"Failed to write node file {file_path}: {e}")

    # Generate companion .md file
    generate_companion_md(node_data)

    return str(file_path)


def generate_companion_md(node_data: Dict) -> str:
    """
    Generate a human-readable .md companion file for a neuron.
    
    The .md file is saved at `neurons/<shard>/[UUID].md`.
    """
    sys = node_data.get("system", {})
    header = node_data.get("header", {})
    node_id = sys.get("id")
    if not node_id:
        return ""

    title = header.get("canonical_name", "Unnamed Neuron")
    node_type = header.get("type", "Unknown")
    aliases = header.get("aliases", [])
    tags = node_data.get("tags", [])
    
    md_content = f"# {title}\n\n"
    md_content += f"**ID:** `{node_id}`  \n"
    md_content += f"**Type:** {node_type}  \n"
    
    if aliases:
        md_content += f"**Aliases:** {', '.join(aliases)}  \n"
    if tags:
        md_content += f"**Tags:** {', '.join(tags)}  \n"
    
    # Descriptions
    descriptions = node_data.get("descriptions", {})
    if descriptions:
        md_content += "\n## Descriptions\n"
        for key, val in descriptions.items():
            if val:
                md_content += f"- **{key.capitalize()}:** {val}\n"
            
    # Properties
    properties = node_data.get("properties", {})
    if properties:
        md_content += "\n## Properties\n"
        for key, vals in properties.items():
            if isinstance(vals, list):
                for v in vals:
                    val_str = v.get("value") if isinstance(v, dict) else str(v)
                    md_content += f"- **{key}:** {val_str}\n"
            else:
                md_content += f"- **{key}:** {vals}\n"

    # Links
    links = node_data.get("links", [])
    if links:
        md_content += "\n## Links\n"
        for link in links:
            target_id = link.get("target_id")
            predicate = link.get("predicate", "related_to")
            # Link to the target's companion .md in its own shard folder.
            # Both files live under neurons/<prefix>/, so a sibling-shard
            # relative path works from any shard: ../<prefix>/<uuid>.md
            if target_id:
                rel = f"../{_shard(target_id)}/{target_id}.md"
                md_content += f"- {predicate}: [{target_id}]({rel})\n"

    md_content += f"\n---\n*Auto-generated companion file for [{node_id}.json]({node_id}.json)*\n"

    md_path = neuron_md_path(node_id)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)
    except OSError as e:
        # Log error but don't fail the whole save process?
        # For now, let's just raise it as we want it to be reliable.
        raise IOError(f"Failed to write companion .md file {md_path}: {e}")

    return str(md_path)


def load_node(uuid_str: str) -> Dict:
    """
    Load a node's JSON data from `neurons/<shard>/[UUID].json`.

    Args:
        uuid_str: The UUID of the node (with or without .json extension).

    Returns:
        The parsed node dictionary.

    Raises:
        FileNotFoundError: If the node file does not exist.
        JSONDecodeError: If the file contains invalid JSON.
    """
    _ensure_neurons_dir()
    # Strip .json suffix if present
    if uuid_str.endswith(".json"):
        uuid_str = uuid_str[:-5]
    file_path = neuron_json_path(uuid_str)
    if not file_path.is_file():
        # Fallback to a legacy flat layout for resilience during migrations.
        legacy = NEURONS_DIR / f"{uuid_str}.json"
        if legacy.is_file():
            file_path = legacy
        else:
            raise FileNotFoundError(f"Node file not found: {file_path}")
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data


def update_node(uuid_str: str, updates: Dict) -> bool:
    """
    Apply partial updates to an existing node while preserving deterministic formatting.

    This function loads the node, performs a recursive deep merge,
    refreshes `system.last_updated`, and saves the node again.

    Args:
        uuid_str: The UUID of the node to update.
        updates: A dictionary with the fields to update.

    Returns:
        True if the update succeeded, False otherwise.

    Raises:
        FileNotFoundError: If the node does not exist.
        ValueError: If the updates would violate the schema.
    """
    try:
        node = load_node(uuid_str)
    except FileNotFoundError:
        raise

    # Deep merge updates
    node = _deep_merge(node, updates)

    # Ensure last_updated is refreshed
    if "system" not in node:
        node["system"] = {}
    node["system"]["last_updated"] = now_iso()

    # Save back
    save_node(node)
    return True


def delete_node(uuid_str: str, archive: bool = False) -> bool:
    """
    Remove the node file (optionally move to an archive).

    Args:
        uuid_str: The UUID of the node to delete.
        archive: If True, move the file to a `.deleted/` subdirectory instead of
                 permanent deletion. Not implemented in V1.

    Returns:
        True if the node was removed, False if it didn't exist.
    """
    _ensure_neurons_dir()
    if uuid_str.endswith(".json"):
        uuid_str = uuid_str[:-5]
    file_path = neuron_json_path(uuid_str)
    if not file_path.is_file():
        return False
    md_path = neuron_md_path(uuid_str)
    if archive:
        # Optional archiving logic
        archive_dir = NEURONS_DIR / ".deleted"
        archive_dir.mkdir(parents=True, exist_ok=True)
        file_path.rename(archive_dir / f"{uuid_str}.json")
        if md_path.is_file():
            md_path.rename(archive_dir / f"{uuid_str}.md")
    else:
        file_path.unlink()
        if md_path.is_file():
            md_path.unlink()
    return True


def list_nodes(node_type: Optional[str] = None) -> List[str]:
    """
    List all node UUIDs (optionally filtered by type).

    Args:
        node_type: If provided, only return nodes of this type
                   (Who, What, Where, When, Why, How).

    Returns:
        List of UUID strings (without .json extension).
    """
    _ensure_neurons_dir()
    uuids = []
    # Recurse through shard sub-folders; skip the archive of deleted nodes.
    for file in NEURONS_DIR.rglob("*.json"):
        if ".deleted" in file.parts:
            continue
        try:
            with open(file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if node_type is None or data.get("header", {}).get("type") == node_type:
                    uuids.append(file.stem)
        except (json.JSONDecodeError, KeyError):
            # Skip corrupted files
            continue
    return sorted(uuids)


if __name__ == "__main__":
    # Simple demonstration
    print("Knewrall CRUD module loaded.")
    print(f"neurons directory: {NEURONS_DIR.absolute()}")
    print(f"Directory exists: {NEURONS_DIR.exists()}")