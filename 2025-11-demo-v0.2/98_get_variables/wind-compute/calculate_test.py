"""
커맨드라인에서 U, V 값을 받아 풍속/풍향 계산
사용법: python calculate_test.py <u_value> <v_value>
예제: python calculate_test.py 85 0
"""
import sys
import numpy as np

def calculate_wind(u, v):
    """
    U, V 성분으로부터 풍속과 풍향 계산
    
    Parameters
    ----------
    u : float
        eastward_wind (U 성분)
    v : float
        northward_wind (V 성분)
        
    Returns
    -------
    tuple
        (풍속, 풍향)
    """
    # 풍속 계산
    speed = np.hypot(u, v)
    
    # 풍향 계산 (TO direction: 바람이 부는 방향)
    direction = (90.0 - np.degrees(np.arctan2(v, u))) % 360.0
    
    return speed, direction


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("사용법: python calculate_test.py <u_value> <v_value>")
        print("예제: python calculate_test.py 85 0")
        print("예제: python calculate_test.py -5.5 3.2")
        sys.exit(1)
    
    try:
        u = float(sys.argv[1])
        v = float(sys.argv[2])
        
        speed, direction = calculate_wind(u, v)
        
        print("=" * 60)
        print(f"입력값:")
        print(f"  U (eastward_wind)  = {u:>8.2f} m/s")
        print(f"  V (northward_wind) = {v:>8.2f} m/s")
        print("-" * 60)
        print(f"계산 결과:")
        print(f"  풍속 (Wind Speed)     = {speed:>8.2f} m/s")
        print(f"  풍향 (Wind Direction) = {direction:>8.2f}°")
        print("=" * 60)
        
    except ValueError:
        print("오류: U와 V 값은 숫자여야 합니다.")
        print("예제: python calculate_test.py 85 0")
        sys.exit(1)