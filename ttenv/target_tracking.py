"""Target Tracking Environments for Reinforcement Learning. OpenAI gym format

[Vairables]

d: radial coordinate of a belief target in the learner frame
alpha : angular coordinate of a belief target in the learner frame
ddot : radial velocity of a belief target in the learner frame
alphadot : angular velocity of a belief target in the learner frame
Sigma : Covariance of a belief target
o_d : linear distance to the closet obstacle point
o_alpha : angular distance to the closet obstacle point

[Environment Descriptions]

TargetTrackingEnv0 : Static Target model + noise - No Velocity Estimate
    RL state: [d, alpha, logdet(Sigma), observed] * nb_targets , [o_d, o_alpha]
    Target: Static [x,y] + noise
    Belief Target: KF, Estimate only x and y

TargetTrackingEnv1 : Double Integrator Target model with KF belief tracker
    RL state: [d, alpha, ddot, alphadot, logdet(Sigma), observed] * nb_targets, [o_d, o_alpha]
    Target : Double Integrator model, [x,y,xdot,ydot]
    Belief Target : KF, Double Integrator model

TargetTrackingEnv2 : Predefined target paths with KF belief tracker
    RL state: [d, alpha, ddot, alphadot, logdet(Sigma), observed] * nb_targets, [o_d, o_alpha]
    Target : Pre-defined target paths - input files required
    Belief Target : KF, Double Integrator model

TargetTrackingEnv3 : SE2 Target model with UKF belief tracker
    RL state: [d, alpha, logdet(Sigma), observed] * nb_targets, [o_d, o_alpha]
    Target : SE2 model [x,y,theta] + a control policy u=[v,w]
    Belief Target : UKF for SE2 model [x,y,theta]

TargetTrackingEnv4 : SE2 Target model with UKF belief tracker [x,y,theta,v,w]
    RL state: [d, alpha, ddot, alphadot, logdet(Sigma), observed] * nb_targets, [o_d, o_alpha]
    Target : SE2 model [x,y,theta] + a control policy u=[v,w]
    Belief Target : UKF for SE2Vel model [x,y,theta,v,w]
"""
import gym
from gym import spaces, logger
from gym.utils import seeding

import numpy as np
from numpy import linalg as LA
import os, copy

from ttenv.maps import map_utils
from ttenv.agent_models import *
from ttenv.policies import *
from ttenv.belief_tracker import KFbelief, UKFbelief
from ttenv.metadata import METADATA
import ttenv.util as util
from ttenv.base import TargetTrackingBase

class TargetTrackingEnv0(TargetTrackingBase):
    def __init__(self, num_targets=1, map_name='empty', is_training=True,
                                                    known_noise=True, **kwargs):
        TargetTrackingBase.__init__(self, num_targets=num_targets, map_name=map_name,
            is_training=is_training, known_noise=known_noise, **kwargs)
        self.id = 'TargetTracking-v0'
        self.target_dim = 2

        # Set limits.
        self.set_limits()

        # Build an agent, targets, and beliefs.
        self.build_models(const_q=METADATA['const_q'], known_noise=known_noise)

    def reset(self, **kwargs):
        if 'const_q' in kwargs:
            self.build_models(const_q=kwargs['const_q'])

        # Reset the agent, targets, and beliefs with sampled initial positions.
        init_pose = super().reset(**kwargs)
        self.agent.reset(init_pose['agent'])
        for i in range(self.num_targets):
            self.belief_targets[i].reset(
                        init_state=init_pose['belief_targets'][i][:self.target_dim],
                        init_cov=self.target_init_cov)
            self.targets[i].reset(np.array(init_pose['targets'][i][:self.target_dim]))

        # The targets are observed by the agent (z_0) and the beliefs are updated (b_0).
        observed = self.observe_and_update_belief()

        # Predict the target for the next step, b_1|0.
        self.belief_targets[i].predict()

        # Compute the RL state.
        self.state_func([0.0, 0.0], observed)
        return self.state

    def state_func(self, action_vw, observed):
        # Find the closest obstacle coordinate.
        obstacles_pt = self.MAP.get_closest_obstacle(self.agent.state)
        if obstacles_pt is None:
            obstacles_pt = (self.sensor_r, np.pi)

        self.state = []
        for i in range(self.num_targets):
            r_b, alpha_b = util.relative_distance_polar(
                                            self.belief_targets[i].state[:2],
                                                xy_base=self.agent.state[:2],
                                                theta_base=self.agent.state[2])
            self.state.extend([r_b, alpha_b,
                                    np.log(LA.det(self.belief_targets[i].cov)),
                                    float(observed[i])])
        self.state.extend([obstacles_pt[0], obstacles_pt[1]])
        self.state = np.array(self.state)

        # Update the visit map for the evaluation purpose.
        if self.MAP.visit_map is not None:
            self.MAP.update_visit_freq_map(self.agent.state, 1.0, observed=bool(np.mean(observed)))

    def set_limits(self):
        self.limit = {} # 0: low, 1:high
        self.limit['agent'] = [np.concatenate((self.MAP.mapmin,[-np.pi])), np.concatenate((self.MAP.mapmax, [np.pi]))]
        self.limit['target'] = [self.MAP.mapmin, self.MAP.mapmax]
        self.limit['state'] = [np.concatenate(([0.0, -np.pi, -50.0, 0.0]*self.num_targets, [0.0, -np.pi ])),
                               np.concatenate(([600.0, np.pi, 50.0, 2.0]*self.num_targets, [self.sensor_r, np.pi]))]
        self.observation_space = spaces.Box(self.limit['state'][0], self.limit['state'][1], dtype=np.float32)

    def build_models(self, const_q=None, known_noise=True, **kwargs):
        if const_q is None:
            self.const_q = np.random.choice([0.001, 0.1, 1.0])
        else:
            self.const_q = const_q

        # Build a robot
        self.agent = AgentSE2(dim=3, sampling_period=self.sampling_period, limit=self.limit['agent'],
                            collision_func=lambda x: self.MAP.is_collision(x))

        self.target_noise_cov = self.const_q * self.sampling_period**3 / 3 * np.eye(self.target_dim)
        if known_noise:
            self.target_true_noise_sd = self.target_noise_cov
        else:
            self.target_true_noise_sd = METADATA['const_q_true'] * np.eye(2)
        self.targetA = np.eye(self.target_dim)

        # Build a target
        self.targets = [AgentDoubleInt2D(dim=self.target_dim, sampling_period=self.sampling_period,
                            limit=self.limit['target'],
                            collision_func=lambda x: self.MAP.is_collision(x),
                            A=self.targetA, W=self.target_true_noise_sd) for _ in range(self.num_targets)]
        self.belief_targets = [KFbelief(dim=self.target_dim, limit=self.limit['target'], A=self.targetA,
                            W=self.target_noise_cov, obs_noise_func=self.observation_noise,
                            collision_func=lambda x: self.MAP.is_collision(x))
                                for _ in range(self.num_targets)]

class TargetTrackingEnv1(TargetTrackingBase):
    def __init__(self, num_targets=1, map_name='empty', is_training=True, known_noise=True, **kwargs):
        TargetTrackingBase.__init__(self, num_targets=num_targets, map_name=map_name,
            is_training=is_training, known_noise=known_noise, **kwargs)
        self.id = 'TargetTracking-v1'
        self.target_dim = 4
        self.target_init_vel = np.array(METADATA['target_init_vel'])

        # Set limits.
        self.set_limits(target_speed_limit=METADATA['target_speed_limit'])

        # Build an agent, targets, and beliefs.
        self.build_models(const_q=METADATA['const_q'], known_noise=known_noise)

    def reset(self, **kwargs):
        if 'target_speed_limit' in kwargs:
            self.set_limits(target_speed_limit=kwargs['target_speed_limit'])

        if 'const_q' in kwargs:
            self.build_models(const_q=kwargs['const_q'])

        # Reset the agent, targets, and beliefs with sampled initial positions.
        init_pose = super().reset(**kwargs)
        self.agent.reset(init_pose['agent'])
        for i in range(self.num_targets):
            self.belief_targets[i].reset(
                        init_state=np.concatenate((init_pose['belief_targets'][i][:2], np.zeros(2))),
                        init_cov=self.target_init_cov)
            self.targets[i].reset(np.concatenate((init_pose['targets'][i][:2], self.target_init_vel)))

        # The targets are observed by the agent (z_0) and the beliefs are updated (b_0).
        observed = self.observe_and_update_belief()

        # Predict the target for the next step, b_1|0.
        self.belief_targets[i].predict()

        # Compute the RL state.
        self.state_func([0.0, 0.0], observed)
        return self.state

    def state_func(self, action_vw, observed):
        # Find the closest obstacle coordinate.
        obstacles_pt = self.MAP.get_closest_obstacle(self.agent.state)
        if obstacles_pt is None:
            obstacles_pt = (self.sensor_r, np.pi)

        self.state = []
        for i in range(self.num_targets):
            r_b, alpha_b = util.relative_distance_polar(self.belief_targets[i].state[:2],
                                                xy_base=self.agent.state[:2],
                                                theta_base=self.agent.state[2])
            r_dot_b, alpha_dot_b = util.relative_velocity_polar(
                                    self.belief_targets[i].state[:2],
                                    self.belief_targets[i].state[2:],
                                    self.agent.state[:2], self.agent.state[2],
                                    action_vw[0], action_vw[1])
            self.state.extend([r_b, alpha_b, r_dot_b, alpha_dot_b,
                                    np.log(LA.det(self.belief_targets[i].cov)),
                                    float(observed[i])])
        self.state.extend([obstacles_pt[0], obstacles_pt[1]])
        self.state = np.array(self.state)

        # Update the visit map for the evaluation purpose.
        if self.MAP.visit_map is not None:
            self.MAP.update_visit_freq_map(self.agent.state, 1.0, observed=bool(np.mean(observed)))

    def set_limits(self, target_speed_limit=None):
        if target_speed_limit is None:
            self.target_speed_limit = np.random.choice([1.0, 3.0])
        else:
            self.target_speed_limit = target_speed_limit
        rel_speed_limit = self.target_speed_limit + METADATA['action_v'][0] # Maximum relative speed

        self.limit = {} # 0: low, 1:highs
        self.limit['agent'] = [np.concatenate((self.MAP.mapmin,[-np.pi])), np.concatenate((self.MAP.mapmax, [np.pi]))]
        self.limit['target'] = [np.concatenate((self.MAP.mapmin,[-self.target_speed_limit, -self.target_speed_limit])),
                                np.concatenate((self.MAP.mapmax, [self.target_speed_limit, self.target_speed_limit]))]
        self.limit['state'] = [np.concatenate(([0.0, -np.pi, -rel_speed_limit, -10*np.pi, -50.0, 0.0]*self.num_targets, [0.0, -np.pi])),
                               np.concatenate(([600.0, np.pi, rel_speed_limit, 10*np.pi,  50.0, 2.0]*self.num_targets, [self.sensor_r, np.pi]))]
        self.observation_space = spaces.Box(self.limit['state'][0], self.limit['state'][1], dtype=np.float32)

    def build_models(self, const_q=None, known_noise=True, **kwargs):
        if const_q is None:
            self.const_q = np.random.choice([0.001, 0.1, 1.0])
        else:
            self.const_q = const_q

        # Build a robot
        self.agent = AgentSE2(dim=3, sampling_period=self.sampling_period, limit=self.limit['agent'],
                            collision_func=lambda x: self.MAP.is_collision(x))

        # Build targets
        self.targetA = np.concatenate((np.concatenate((np.eye(2), self.sampling_period*np.eye(2)), axis=1),
                                        [[0,0,1,0],[0,0,0,1]]))
        self.target_noise_cov = self.const_q * np.concatenate((
                            np.concatenate((self.sampling_period**3/3*np.eye(2), self.sampling_period**2/2*np.eye(2)), axis=1),
                        np.concatenate((self.sampling_period**2/2*np.eye(2), self.sampling_period*np.eye(2)),axis=1) ))
        if known_noise:
            self.target_true_noise_sd = self.target_noise_cov
        else:
            self.target_true_noise_sd = self.const_q_true * np.concatenate((
                        np.concatenate((self.sampling_period**2/2*np.eye(2), self.sampling_period/2*np.eye(2)), axis=1),
                        np.concatenate((self.sampling_period/2*np.eye(2), self.sampling_period*np.eye(2)),axis=1) ))

        self.targets = [AgentDoubleInt2D_Nonlinear(self.target_dim, self.sampling_period, self.limit['target'],
                            lambda x: self.MAP.is_collision(x),
                            W=self.target_true_noise_sd, A=self.targetA,
                            obs_check_func=lambda x: self.MAP.get_closest_obstacle(
                                x, fov=2*np.pi, r_max=10e2)) for _ in range(self.num_targets)]
        self.belief_targets = [KFbelief(dim=self.target_dim, limit=self.limit['target'], A=self.targetA,
                            W=self.target_noise_cov, obs_noise_func=self.observation_noise,
                            collision_func=lambda x: self.MAP.is_collision(x))
                            for _ in range(self.num_targets)]

class TargetTrackingEnv2(TargetTrackingEnv1):
    def __init__(self, num_targets=1, map_name='empty', is_training=True,
                known_noise=True, target_path_dir=None, **kwargs):
        """
        A predefined path for each target must be provided under the target_path_dir.
        Each path_i file for i=target_num is a T by 4 matrix where T is the
        number of time steps in a trajectory (or per episode). Each row consists
        of (x, y, xdot, ydot).
        """
        if target_path_dir is None:
            raise ValueError('No path directory for targets is provided.')
        TargetTrackingEnv1.__init__(self, num_targets=num_targets,
            map_name=map_name, is_training=is_training, known_noise=known_noise, **kwargs)
        self.id = 'TargetTracking-v2'
        self.targets = [Agent2DFixedPath(dim=self.target_dim, sampling_period=self.sampling_period,
                                limit=self.limit['target'],
                                collision_func=lambda x: self.MAP.is_collision(x),
                                path=np.load(os.path.join(target_path_dir, "path_%d.npy"%(i+1)))) for i in range(self.num_targets)]
    def reset(self, **kwargs):
        self.state = []
        if self.MAP.map is None:
            a_init = self.agent_init_pos[:2]
            self.agent.reset(self.agent_init_pos)
        else:
            isvalid = False
            while(not isvalid):
                a_init = np.random.random((2,)) * (self.MAP.mapmax-self.MAP.mapmin) + self.MAP.mapmin
                isvalid = not(self.MAP.is_collision(a_init))
            self.agent.reset([a_init[0], a_init[1], np.random.random()*2*np.pi-np.pi])
        for i in range(self.num_targets):
            t_init = np.load("path_sh_%d.npy"%(i+1))[0][:2]
            self.belief_targets[i].reset(init_state=np.concatenate((t_init + METADATA['init_distance_belief'] * (np.random.rand(2)-0.5), np.zeros(2))), init_cov=self.target_init_cov)
            self.targets[i].reset(np.concatenate((t_init, self.target_init_vel)))
            r, alpha = util.relative_distance_polar(self.belief_targets[i].state[:2],
                                                xy_base=self.agent.state[:2],
                                                theta_base=self.agent.state[2])
            logdetcov = np.log(LA.det(self.belief_targets[i].cov))
            self.state.extend([r, alpha, 0.0, 0.0, logdetcov, 0.0])
        self.state.extend([self.sensor_r, np.pi])
        self.state = np.array(self.state)
        return self.state


class TargetTrackingEnv3(TargetTrackingEnv0):
    def __init__(self, num_targets=1, map_name='empty', is_training=True, known_noise=True, **kwargs):
        TargetTrackingEnv0.__init__(self, num_targets=num_targets,
            map_name=map_name, is_training=is_training, known_noise=known_noise, **kwargs)
        self.id = 'TargetTracking-v3'
        self.target_dim = 3

        # LIMIT
        self.limit = {} # 0: low, 1:highs
        self.limit['agent'] = [np.concatenate((self.MAP.mapmin,[-np.pi])), np.concatenate((self.MAP.mapmax, [np.pi]))]
        self.limit['target'] = [np.concatenate((self.MAP.mapmin, [-np.pi])), np.concatenate((self.MAP.mapmax, [np.pi]))]
        self.limit['state'] = [np.concatenate(([0.0, -np.pi, -50.0, 0.0]*num_targets, [0.0, -np.pi ])),
                               np.concatenate(([600.0, np.pi, 50.0, 2.0]*num_targets, [self.sensor_r, np.pi]))]
        self.observation_space = spaces.Box(self.limit['state'][0], self.limit['state'][1], dtype=np.float32)
        self.target_noise_cov = METADATA['const_q'] * self.sampling_period * np.eye(self.target_dim)
        if known_noise:
            self.target_true_noise_sd = self.target_noise_cov
        else:
            self.target_true_noise_sd = METADATA['const_q_true'] * \
                                self.sampling_period * np.eye(self.target_dim)
        # Build a robot
        self.agent = AgentSE2(3, self.sampling_period, self.limit['agent'],
                            lambda x: self.MAP.is_collision(x))
        # Build a target
        self.targets = [AgentSE2(self.target_dim, self.sampling_period, self.limit['target'],
                            lambda x: self.MAP.is_collision(x),
                            policy=SinePolicy(0.1, 0.5, 5.0, self.sampling_period)) for _ in range(num_targets)]
        # SinePolicy(0.5, 0.5, 2.0, self.sampling_period)
        # CirclePolicy(self.sampling_period, self.MAP.origin, 3.0)
        # RandomPolicy()

        self.belief_targets = [UKFbelief(dim=self.target_dim, limit=self.limit['target'], fx=SE2Dynamics,
                            W=self.target_noise_cov, obs_noise_func=self.observation_noise,
                            collision_func=lambda x: self.MAP.is_collision(x))
                            for _ in range(num_targets)]

    def step(self, action):
        action_vw = self.action_map[action]
        is_col = self.agent.update(action_vw, [t.state[:2] for t in self.targets])
        obstacles_pt = self.MAP.get_closest_obstacle(self.agent.state)
        observed = []
        for i in range(self.num_targets):
            self.targets[i].update()

            # Observe
            obs = self.observation(self.targets[i])
            observed.append(obs[0])
            # Update the belief of the agent on the target using UKF
            self.belief_targets[i].update(obs[0], obs[1], self.agent.state,
                                        np.array([np.random.random(),
                                        np.pi*np.random.random()-0.5*np.pi]))

        reward, done, mean_nlogdetcov = self.get_reward(self.is_training, is_col=is_col)
        self.state = []
        if obstacles_pt is None:
            obstacles_pt = (self.sensor_r, np.pi)
        for i in range(self.num_targets):
            r_b, alpha_b = util.relative_distance_polar(self.belief_targets[i].state[:2],
                                                xy_base=self.agent.state[:2],
                                                theta_base=self.agent.state[2])
            self.state.extend([r_b, alpha_b,
                                np.log(LA.det(self.belief_targets[i].cov)), float(observed[i])])
        self.state.extend([obstacles_pt[0], obstacles_pt[1]])
        self.state = np.array(self.state)
        return self.state, reward, done, {'mean_nlogdetcov': mean_nlogdetcov}

class TargetTrackingEnv4(TargetTrackingEnv0):
    def __init__(self, num_targets=1, map_name='empty', is_training=True, known_noise=True, **kwargs):
        TargetTrackingEnv0.__init__(self, num_targets=num_targets,
            map_name=map_name, is_training=is_training, known_noise=known_noise, **kwargs)
        self.id = 'TargetTracking-v4'
        self.target_dim = 5
        self.target_init_vel = np.array(METADATA['target_init_vel'])

        # LIMIT
        self.limit = {} # 0: low, 1:highs
        rel_speed_limit = METADATA['target_speed_limit'] + METADATA['action_v'][0] # Maximum relative speed
        self.limit['agent'] = [np.concatenate((self.MAP.mapmin,[-np.pi])), np.concatenate((self.MAP.mapmax, [np.pi]))]
        self.limit['target'] = [np.concatenate((self.MAP.mapmin, [-np.pi, -METADATA['target_speed_limit'], -np.pi])),
                                            np.concatenate((self.MAP.mapmax, [np.pi, METADATA['target_speed_limit'], np.pi]))]
        self.limit['state'] = [np.concatenate(([0.0, -np.pi, -rel_speed_limit, -10*np.pi, -50.0, 0.0]*num_targets, [0.0, -np.pi ])),
                               np.concatenate(([600.0, np.pi, rel_speed_limit, 10*np.pi, 50.0, 2.0]*num_targets, [self.sensor_r, np.pi]))]
        self.observation_space = spaces.Box(self.limit['state'][0], self.limit['state'][1], dtype=np.float32)
        self.target_noise_cov = np.zeros((self.target_dim, self.target_dim))
        for i in range(3):
            self.target_noise_cov[i,i] = METADATA['const_q'] * self.sampling_period**3/3
        self.target_noise_cov[3:, 3:] = METADATA['const_q'] * \
                    np.array([[self.sampling_period, self.sampling_period**2/2],
                             [self.sampling_period**2/2, self.sampling_period]])
        if known_noise:
            self.target_true_noise_sd = self.target_noise_cov
        else:
            self.target_true_noise_sd = METADATA['const_q_true'] * \
                                  self.sampling_period * np.eye(self.target_dim)
        # Build a robot
        self.agent = AgentSE2(3, self.sampling_period, self.limit['agent'],
                            lambda x: self.MAP.is_collision(x))
        # Build a target
        self.targets = [AgentSE2(self.target_dim, self.sampling_period, self.limit['target'],
                            lambda x: self.MAP.is_collision(x),
                            policy=ConstantPolicy(self.target_noise_cov[3:, 3:])) for _ in range(num_targets)]
        # SinePolicy(0.5, 0.5, 2.0, self.sampling_period)
        # CirclePolicy(self.sampling_period, self.MAP.origin, 3.0)
        # RandomPolicy()

        self.belief_targets = [UKFbelief(dim=self.target_dim, limit=self.limit['target'], fx=SE2DynamicsVel,
                            W=self.target_noise_cov, obs_noise_func=self.observation_noise,
                            collision_func=lambda x: self.MAP.is_collision(x))
                            for _ in range(num_targets)]

    def reset(self, **kwargs):
        self.state = []
        init_pose = self.get_init_pose(**kwargs)
        self.agent.reset(init_pose['agent'])
        for i in range(self.num_targets):
            self.belief_targets[i].reset(
                        init_state=np.concatenate((init_pose['belief_targets'][i], np.zeros(2))),
                        init_cov=self.target_init_cov)
            t_init = np.concatenate((init_pose['targets'][i], [self.target_init_vel[0], 0.0]))
            self.targets[i].reset(t_init)
            self.targets[i].policy.reset(t_init)
            r, alpha = util.relative_distance_polar(self.belief_targets[i].state[:2],
                                                xy_base=self.agent.state[:2],
                                                theta_base=self.agent.state[2])
            logdetcov = np.log(LA.det(self.belief_targets[i].cov))
            self.state.extend([r, alpha, 0.0, 0.0, logdetcov, 0.0])
        self.state.extend([self.sensor_r, np.pi])
        self.state = np.array(self.state)
        return self.state

    def step(self, action):
        action_vw = self.action_map[action]
        is_col = self.agent.update(action_vw, [t.state[:2] for t in self.targets])
        obstacles_pt = self.MAP.get_closest_obstacle(self.agent.state)
        observed = []
        for i in range(self.num_targets):
            self.targets[i].update()
            # Observe
            obs = self.observation(self.targets[i])
            observed.append(obs[0])
            # Update the belief of the agent on the target using UKF
            self.belief_targets[i].update(obs[0], obs[1], self.agent.state,
             np.array([np.random.random(), np.pi*np.random.random()-0.5*np.pi]))

        reward, done, mean_nlogdetcov = self.get_reward(self.is_training, is_col=is_col)
        self.state = []
        if obstacles_pt is None:
            obstacles_pt = (self.sensor_r, np.pi)
        for i in range(self.num_targets):
            r_b, alpha_b = util.relative_distance_polar(self.belief_targets[i].state[:2],
                                                xy_base=self.agent.state[:2],
                                                theta_base=self.agent.state[2])
            r_dot_b, alpha_dot_b = util.relative_velocity_polar_se2(
                                    self.belief_targets[i].state[:3],
                                    self.belief_targets[i].state[3:],
                                    self.agent.state, action_vw)
            self.state.extend([r_b, alpha_b, r_dot_b, alpha_dot_b,
                                    np.log(LA.det(self.belief_targets[i].cov)), float(observed[i])])
        self.state.extend([obstacles_pt[0], obstacles_pt[1]])
        self.state = np.array(self.state)
        return self.state, reward, done, {'mean_nlogdetcov': mean_nlogdetcov}
