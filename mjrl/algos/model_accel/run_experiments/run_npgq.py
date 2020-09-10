"""
Job script to optimize policy with fitted model
"""

from os import environ
environ['CUDA_DEVICE_ORDER']='PCI_BUS_ID'
environ['MKL_THREADING_LAYER']='GNU'
import numpy as np
import copy
import torch
import torch.nn as nn
import pickle
import mjrl.envs
import time as timer
import argparse
import os
import json
import mjrl.samplers.core as sampler
import mjrl.utils.tensor_utils as tensor_utils
from tqdm import tqdm
from tabulate import tabulate
from mjrl.policies.gaussian_mlp import MLP
from mjrl.baselines.mlp_baseline import MLPBaseline
from mjrl.baselines.quadratic_baseline import QuadraticBaseline
from mjrl.baselines.mlp_q_baseline import MLPQBaseline
from mjrl.utils.gym_env import GymEnv
from mjrl.utils.logger import DataLog
from mjrl.utils.make_train_plots import make_train_plots
from mjrl.utils.replay_buffer import ReplayBuffer
from mjrl.algos.model_accel.nn_dynamics import WorldModel
from mjrl.algos.model_accel.npg_sac import NPGSAC
from mjrl.algos.model_accel.sampling import sample_paths, evaluate_policy


# ===============================================================================
# Get command line arguments
# ===============================================================================

parser = argparse.ArgumentParser(description='Model accelerated policy optimization.')
parser.add_argument('--output', type=str, required=True, help='location to store results')
parser.add_argument('--config', type=str, required=True, help='path to config file with exp params')
args = parser.parse_args()
OUT_DIR = args.output
if not os.path.exists(OUT_DIR):
    os.mkdir(OUT_DIR)
with open(args.config, 'r') as f:
    job_data = eval(f.read())

# Unpack args and make files for easy access
logger = DataLog()
ENV_NAME = job_data['env_name']
EXP_FILE = OUT_DIR + '/job_data.json'
SEED = job_data['seed']

# base cases
if 'eval_rollouts' not in job_data.keys():  job_data['eval_rollouts'] = 0
if 'save_freq' not in job_data.keys():      job_data['save_freq'] = 10
if 'device' not in job_data.keys():         job_data['device'] = 'cpu'
if 'hvp_frac' not in job_data.keys():       job_data['hvp_frac'] = 1.0
if 'start_state' not in job_data.keys():    job_data['start_state'] = 'init'
if 'learn_reward' not in job_data.keys():   job_data['learn_reward'] = True
if 'replay_buffer_size' not in job_data.keys():   job_data['replay_buffer_size'] = int(1e6)

assert job_data['start_state'] in ['init', 'buffer']
with open(EXP_FILE, 'w') as f:  json.dump(job_data, f, indent=4)
del(job_data['seed'])
job_data['base_seed'] = SEED


# ===============================================================================
# Helper functions
# ===============================================================================
def buffer_size(paths_list):
    return np.sum([p['observations'].shape[0]-1 for p in paths_list])


# ===============================================================================
# Train loop
# ===============================================================================

np.random.seed(SEED)
torch.random.manual_seed(SEED)

e = GymEnv(ENV_NAME)
e.set_seed(SEED)

models = [WorldModel(state_dim=e.observation_dim, act_dim=e.action_dim, seed=SEED+i, 
                     **job_data) for i in range(job_data['num_models'])]
policy = MLP(e.spec, seed=SEED, hidden_sizes=job_data['policy_size'], 
                init_log_std=job_data['init_log_std'], min_log_std=job_data['min_log_std'])
if 'init_policy' in job_data.keys():
    if job_data['init_policy'] != None: policy = pickle.load(open(job_data['init_policy'], 'rb'))
baseline = MLPQBaseline(e.spec, reg_coef=1e-3, batch_size=256, epochs=2,  learn_rate=1e-3,
                       use_gpu=(True if job_data['device'] == 'cuda' else False))               
agent = NPGSAC(learned_model=models, env=e, policy=policy, baseline=baseline, seed=SEED,
                      # hvp_sample_frac=job_data['hvp_frac'],
                      normalized_step_size=job_data['step_size'], save_logs=True)
replay_buffer = ReplayBuffer(job_data['replay_buffer_size'])

paths = []
init_states_buffer = []

for outer_iter in range(job_data['num_iter']):

    ts = timer.time()
    print("================> ITERATION : %i " % outer_iter)
    print("Getting interaction data from real dynamics ...")

    samples_to_collect = job_data['init_samples'] if outer_iter == 0 else job_data['iter_samples']
    iter_paths = sampler.sample_data_batch(samples_to_collect, agent.env, 
                    agent.policy, eval_mode=False, base_seed=SEED + outer_iter)
    for p in iter_paths:
        paths.append(p)
        init_states_buffer.append(p['observations'][0])
    while buffer_size(paths) > job_data['buffer_size']:
        paths[:1] = []

    s = np.concatenate([p['observations'][:-1] for p in paths])
    a = np.concatenate([p['actions'][:-1] for p in paths])
    sp = np.concatenate([p['observations'][1:] for p in paths])
    r = np.concatenate([p['rewards'][:-1] for p in paths])
    terminated = np.concatenate([[False] * (len(p['observations']) - 2) + [p['terminated']] for p in paths])
    rollout_score = np.mean([np.sum(p['rewards']) for p in iter_paths])
    num_samples = np.sum([p['rewards'].shape[0] for p in iter_paths])

    replay_buffer.store(s=s, a=a, r=r, sp=sp, terminated=terminated)

    logger.log_kv('fit_epochs', job_data['fit_epochs'])
    logger.log_kv('rollout_score', rollout_score)
    logger.log_kv('iter_samples', num_samples)
    try:
        rollout_metric = e.env.env.evaluate_success(iter_paths)
        logger.log_kv('rollout_metric', rollout_metric)
    except:
        pass

    print("Data gathered, fitting model ...")
    if job_data['refresh_fit']:
        models = [WorldModel(state_dim=e.observation_dim, act_dim=e.action_dim, seed=SEED+123*outer_iter,
                             **job_data) for i in range(job_data['num_models'])]

    for i, model in enumerate(models):
        loss_general = model.compute_loss(s[-samples_to_collect:], 
                       a[-samples_to_collect:], sp[-samples_to_collect:]) # generalization error
        dynamics_loss = model.fit_dynamics(s, a, sp, **job_data)
        logger.log_kv('dyn_loss_' + str(i), dynamics_loss[-1])
        logger.log_kv('dyn_loss_gen_' + str(i), loss_general)
        if job_data['learn_reward']:
            reward_loss = model.fit_reward(s, a, r.reshape(-1, 1), **job_data)
            logger.log_kv('rew_loss_' + str(i), reward_loss[-1])

    # =================================
    # Refresh policy if necessary
    # =================================
    if 'refresh_policy' in job_data.keys():
        # start policy optimization from scratch (note that data has already been collected with an improved policy)
        if job_data['refresh_policy']:
            policy = MLP(e.spec, seed=SEED, hidden_sizes=job_data['policy_size'], 
                init_log_std=job_data['init_log_std'], min_log_std=job_data['min_log_std'])
            agent.policy = policy
        else:
            pass

    # =================================
    # NPG updates
    # =================================
    agent.learned_model = models
    for inner_step in range(job_data['inner_steps']):
        if job_data['start_state'] == 'init':
            print('sampling from initial state distribution')
            buffer_rand_idx = np.random.choice(len(init_states_buffer), size=job_data['update_paths'], replace=True).tolist()
            init_states = [init_states_buffer[idx] for idx in buffer_rand_idx]
        else:
            # Mix data between initial states and randomly sampled data from buffer
            print("sampling from mix of initial states and data buffer")
            if 'buffer_frac' in job_data.keys():
                num_states_1 = int(job_data['update_paths']*(1-job_data['buffer_frac'])) + 1
                num_states_2 = int(job_data['update_paths']* job_data['buffer_frac']) + 1
            else:
                num_states_1, num_states_2 = job_data['update_paths'] // 2, job_data['update_paths'] // 2
            buffer_rand_idx = np.random.choice(len(init_states_buffer), size=num_states_1, replace=True).tolist()
            init_states_1 = [init_states_buffer[idx] for idx in buffer_rand_idx]
            buffer_rand_idx = np.random.choice(s.shape[0], size=num_states_2, replace=True)
            init_states_2 = list(s[buffer_rand_idx])
            init_states = init_states_1 + init_states_2

        agent.train_step(N=len(init_states), init_states=init_states, horizon=job_data['horizon'])
        print_data = sorted(filter(lambda v: np.asarray(v[1]).size == 1,
                                   agent.logger.get_current_log().items()))
        print(tabulate(print_data))

    if job_data['eval_rollouts'] > 0:
        print("Performing validation rollouts ... ")
        eval_paths = evaluate_policy(agent.env, agent.policy, agent.learned_model[0], noise_level=0.0,
                                     real_step=True, num_episodes=job_data['eval_rollouts'], visualize=False)
        eval_score = np.mean([np.sum(p['rewards']) for p in eval_paths])
        logger.log_kv('eval_score', eval_score)
        try:
            eval_metric = e.env.env.evaluate_success(eval_paths)
            logger.log_kv('eval_metric', eval_metric)
        except:
            pass
    else:
        eval_paths = []

    exp_data = dict(log=logger.log, rollout_paths=iter_paths, eval_paths=eval_paths)
    if outer_iter > 0 and outer_iter % job_data['save_freq'] == 0:
        # convert to CPU before pickling
        agent.to('cpu')
        pickle.dump(agent, open(OUT_DIR + '/agent_' + str(outer_iter) + '.pickle', 'wb'))
        pickle.dump(policy, open(OUT_DIR + '/policy_' + str(outer_iter) + '.pickle', 'wb'))
        agent.to(job_data['device'])

    tf = timer.time()
    logger.log_kv('iter_time', tf-ts)
    print_data = sorted(filter(lambda v: np.asarray(v[1]).size == 1,
                               logger.get_current_log().items()))
    print(tabulate(print_data))
    logger.save_log(OUT_DIR+'/')
    make_train_plots(log=logger.log, keys=['rollout_score', 'eval_score', 'rollout_metric', 'eval_metric'],
                     sample_key = 'iter_samples', save_loc=OUT_DIR+'/')

# final save
pickle.dump(agent, open(OUT_DIR + '/agent_final.pickle', 'wb'))
pickle.dump(policy, open(OUT_DIR + '/policy_final.pickle', 'wb'))
