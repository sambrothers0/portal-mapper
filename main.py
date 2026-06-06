from __future__ import annotations

import io
import tempfile
import zipfile

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from parser import find_region_dir, scan_for_block

app = FastAPI()

app.add_middleware(
	CORSMiddleware,
	allow_origins=['*'],
	allow_methods=['*'],
	allow_headers=['*'],
)


@app.post('/parse-blocks')
async def parse_blocks(
	file: UploadFile = File(...),
	block_type: str = Form(default='minecraft:nether_portal'),
):
	if not file.filename or not file.filename.endswith('.zip'):
		raise HTTPException(status_code=400, detail='Upload must be a .zip of the save folder')

	contents = await file.read()

	with tempfile.TemporaryDirectory() as tmpdir:
		try:
			with zipfile.ZipFile(io.BytesIO(contents)) as archive:
				archive.extractall(tmpdir)
		except zipfile.BadZipFile as exc:
			raise HTTPException(status_code=400, detail='File is not a valid zip archive') from exc

		try:
			region_dir = find_region_dir(tmpdir)
		except FileNotFoundError as exc:
			raise HTTPException(status_code=422, detail=str(exc)) from exc

		matches = scan_for_block(region_dir, block_type)

	return {
		'block_type': block_type,
		'count': len(matches),
		'matches': matches,
	}
