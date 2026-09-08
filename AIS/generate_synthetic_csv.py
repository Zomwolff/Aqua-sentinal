import pandas as pd
import random
import sys

def main():
    print("Loading datasets...")
    try:
        sample_df = pd.read_csv("sample-ais.csv")
    except Exception as e:
        print(f"Error loading sample-ais.csv: {e}")
        return
        
    try:
        mumbai_df = pd.read_csv("mumbai-vessels.csv")
    except Exception as e:
        print(f"Error loading mumbai-vessels.csv: {e}")
        return
    
    print("Preparing identities...")
    # Drop rows without MMSI in mumbai data
    mumbai_df = mumbai_df.dropna(subset=['MMSI'])
    # Extract unique identities
    mumbai_vessels = mumbai_df[['MMSI', 'Vessel Name', 'Vessel Type', 'IMO', 'CallSign']].drop_duplicates().to_dict('records')
    
    # Get unique MMSIs in sample data
    sample_mmsis = sample_df['MMSI'].unique()
    
    if len(mumbai_vessels) < len(sample_mmsis):
        print(f"Warning: Only {len(mumbai_vessels)} mumbai vessels for {len(sample_mmsis)} sample trajectories. We will sample with replacement.")
        
    # Map sample MMSI to Mumbai Vessel
    mmsi_mapping = {}
    for sample_mmsi in sample_mmsis:
        vessel = random.choice(mumbai_vessels)
        mmsi_mapping[sample_mmsi] = vessel

    # We need to shift LAT and LON.
    # Mumbai target is roughly 19.0 LAT, 72.5 LON
    center_lat = sample_df['LAT'].mean()
    center_lon = sample_df['LON'].mean()
    
    target_lat = 19.0
    target_lon = 72.5
    
    lat_shift = target_lat - center_lat
    lon_shift = target_lon - center_lon

    print(f"Shifting LAT by {lat_shift:.4f} and LON by {lon_shift:.4f}")
    
    print("Applying mappings and translations (vectorized)...")
    
    mmsi_to_new_mmsi = {k: v['MMSI'] for k, v in mmsi_mapping.items()}
    mmsi_to_name = {k: v['Vessel Name'] for k, v in mmsi_mapping.items()}
    mmsi_to_imo = {k: v['IMO'] for k, v in mmsi_mapping.items()}
    mmsi_to_callsign = {k: v['CallSign'] for k, v in mmsi_mapping.items()}
    mmsi_to_type = {k: v['Vessel Type'] for k, v in mmsi_mapping.items()}
    
    # Map the new identity columns
    sample_df['VesselName'] = sample_df['MMSI'].map(mmsi_to_name)
    sample_df['IMO'] = sample_df['MMSI'].map(mmsi_to_imo)
    sample_df['CallSign'] = sample_df['MMSI'].map(mmsi_to_callsign)
    sample_df['VesselType'] = sample_df['MMSI'].map(mmsi_to_type)
    
    # Remap MMSI (doing this last because it was the mapping key)
    sample_df['MMSI'] = sample_df['MMSI'].map(mmsi_to_new_mmsi)
    
    # Translate coordinates to Mumbai
    sample_df['LAT'] = sample_df['LAT'] + lat_shift
    sample_df['LON'] = sample_df['LON'] + lon_shift

    print("Saving to synthetic-mumbai-ais.csv...")
    sample_df.to_csv("synthetic-mumbai-ais.csv", index=False)
    print("Phase 1 complete! Saved to synthetic-mumbai-ais.csv")

if __name__ == "__main__":
    main()
