import numpy as np
import os
import yaml
from skimage.transform import rotate

from ttenv.maps.map_utils import GridMap
import ttenv.util as util

class DynamicMap(GridMap):
    def __init__(self, obj_lib_path, map_path, margin2wall=0.5):
        """
        Parameters
        ---------
        obj_lib_path : A path to a folder where all obstacle objects are stored.
            The folder must contain only obstacle objects in .npy files.
        """
        map_config = yaml.load(open(map_path+".yaml", "r"))
        self.mapdim = map_config['mapdim']
        self.mapres = np.array(map_config['mapres'])
        self.mapmin = np.array(map_config['mapmin'])
        self.mapmax = np.array(map_config['mapmax'])
        self.margin2wall = margin2wall
        self.origin = map_config['origin']
        self.submap_coordinates = [[map_config['submaporigin'][2*i], map_config['submaporigin'][2*i+1]] for i in range(4)]

        self.obstacles = []
        obj_files = os.listdir(obj_lib_path)
        for obj_f in obj_files:
            if '.npy' in obj_f:
                self.obstacles.append(np.load(os.path.join(obj_lib_path, obj_f)))
        self.visit_freq_map = None
        self.visit_map = None

    def generate_map(self, chosen_idx=None, rot_angs=None, **kwargs):
        self.map = np.zeros(self.mapdim)
        map_tmp = np.zeros(self.mapdim)
        if chosen_idx is None:
            chosen_idx = np.random.choice(len(self.obstacles), 3, replace=False)
            chosen_idx = np.append(chosen_idx, 2)
        if rot_angs is None:
            rot_angs = [np.random.choice(np.arange(-10,10,1) / 10. * 180) for _ in range(4)]
        for (i, c_id) in enumerate(chosen_idx):
            rotated_obs = rotate(self.obstacles[c_id], rot_angs[i], resize=True, center=(24,24))
            rotated_obs_idx_local = np.array(np.nonzero(rotated_obs))
            rotated_obs_idx_global_0 = rotated_obs_idx_local[0] \
                                        - int(rotated_obs.shape[0]/2) \
                                        + self.submap_coordinates[i][0]
            rotated_obs_idx_global_1 = rotated_obs_idx_local[1] \
                                        - int(rotated_obs.shape[1]/2) \
                                        + self.submap_coordinates[i][1]
            self.map[rotated_obs_idx_global_0, rotated_obs_idx_global_1] = 1.0
        self.map_linear = np.squeeze(self.map.astype(np.int8).reshape(-1, 1))
        
        # TEMP : Logging purpose.
        self.chosen_idx = chosen_idx
        self.rot_angs = rot_angs

if __name__ == '__main__':
    print("Test DynamicMap")
    d = DynamicMap(obj_lib_path='maps/lib_obstacles', map_path='maps/dynamic_map')
    for _ in range(5):
        d.generate_map()
        import matplotlib.pyplot as plt
        plt.imshow(d.map, cmap='gray_r')
        plt.show()
    plt.close()
