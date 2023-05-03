This is a python script + nginx config to store and serve approved images with
limited resizing capabilities. 

Image links are signed and expire regularly. Images can be proxied or stored
permanently.

# Prerequisites
 
 * nginx
 * imgproxy (https://github.com/imgproxy/imgproxy)
 * python >= 3.9

# Install

 * edit `/tmp/imgproxy` paths in `nginx/imgproxy.conf` and move file to nginx's `sites-enabled` dir
 * Create directory structure
 
       mkdir images ; for x in {a..z} {A..Z} {0..9} _ - ; do mkdir images/"$x" ; for y in {a..z} {A..Z} {0..9} _ - ; do mkdir images/"$x/$y" ; done ; done

 * in `main.py` change `SIG_SECRET` (same as gazelle's `IMAGE_CACHE_SECRET`)
 
# Run

 * `uvicorn main:app` (or any other ASGI-compatible server)

See `contrib/imageproxy.service` for a systemd service.

# URL format description

URI format: `/i/{img_size}/{signature}/{ext_url}{extension}`

`img_size` can be one of: `full` or `{h}x{w}` (x denotes the literal character "x")
`h` and `w` can be an integer or empty string. The integer must be one of the
preconfigured and allowed sizes (in pixels) in the imageproxy server. If `w`
and `h` are given, the image is resized to the maximum size that keeps the correct
aspect ratio while not exceeding any of the two limits. Suggested values are 150/250/500.

`signature` is the result of `urlsafe-base64(hmac-sha256-trunc(key={IMG_PROXY_SECRET}{current_year}{iso_current_week}, message={ext_url}{extension}))`.
HMAC result is truncated to 12 bytes, producing a 16 characters encoded signature.

`ext_url` is the result of `urlsafe-base64(image_url)`. Trailing `=` may be stripped.

`extension` is either the empty string or the string `/proxy` which instructs
the imageproxy server to not permanently store the source image. Resizing is
not supported in proxy-only mode.

## Signature test vector

```
IMG_PROXY_SECRET="1234"
image_url="https://example_url/img.jpg"
date=1640730000 // (2021-12-29)
```

-> `/i/full/ae0_4RXxKhh6_AwX/aHR0cHM6Ly9leGFtcGxlX3VybC9pbWcuanBn`
