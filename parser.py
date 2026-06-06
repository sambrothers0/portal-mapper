from __future__ import annotations

import os

import anvil


def find_region_dir(base: str) -> str:
	for root, _dirs, files in os.walk(base):
		path_parts = os.path.normpath(root).split(os.sep)
		if path_parts[-1] != 'region' or 'DIM-1' not in path_parts:
			continue

		if any(file_name.endswith('.mca') for file_name in files):
			return root
	raise FileNotFoundError('No Nether region files found in DIM-1. Is this a valid save?')


def _normalize_block_type(block_type: str) -> str:
	if ':' in block_type:
		return block_type.split(':', 1)[1]
	return block_type


def scan_for_block(region_dir: str, block_type: str) -> list[dict[str, int]]:
	target_block = _normalize_block_type(block_type)
	matches: list[dict[str, int]] = []

	for file_name in os.listdir(region_dir):
		if not file_name.endswith('.mca'):
			continue

		region = anvil.Region.from_file(os.path.join(region_dir, file_name))

		for chunk_x in range(32):
			for chunk_z in range(32):
				try:
					chunk = region.get_chunk(chunk_x, chunk_z)
				except Exception:
					continue

				for index, block in enumerate(chunk.stream_blocks(force_new=True)):
					if block.id != target_block:
						continue

					x = index % 16
					z = (index // 16) % 16
					y = index // 256
					matches.append(
						{
							'x': chunk_x * 16 + x,
							'y': y,
							'z': chunk_z * 16 + z,
						}
					)

	return matches
