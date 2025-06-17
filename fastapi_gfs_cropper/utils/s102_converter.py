import numpy as np

def crop_and_convert(data, lats, lons, bbox):
    min_lon, min_lat, max_lon, max_lat = bbox
    cropped = []
    for i in range(data.shape[0]):
        for j in range(data.shape[1]):
            lat = lats[i] if lats.ndim == 1 else lats[i, j]
            lon = lons[j] if lons.ndim == 1 else lons[i, j]
            if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
                cropped.append({
                    "lat": float(lat),
                    "lon": float(lon),
                    "value": float(data[i, j])
                })
    resolution = [abs(lons[1] - lons[0]) if lons.ndim == 1 else abs(lons[0,1] - lons[0,0]),
                  abs(lats[1] - lats[0]) if lats.ndim == 1 else abs(lats[1,0] - lats[0,0])]
    return cropped, resolution