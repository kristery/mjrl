from mjrl.utils.gym_env import GymEnv
from mjrl.policies.gaussian_mlp import MLP
from mjrl.baselines.quadratic_baseline import QuadraticBaseline
from mjrl.baselines.mlp_baseline import MLPBaseline
from mjrl.algos.npg_cg import NPG
from mjrl.utils.train_agent import train_agent
import mjrl.envs
import time as timer
SEED = 500

e = GymEnv('mjrl_point_mass-v0')
policy = MLP(e.spec, hidden_sizes=(32,32), seed=SEED)
baseline = QuadraticBaseline(e.spec)
agent = NPG(e, policy, baseline, normalized_step_size=0.1, seed=SEED, save_logs=True)

ts = timer.time()
train_agent(job_name='point_mass_exp1',
            agent=agent,
            seed=SEED,
            niter=50,
            gamma=0.95,
            gae_lambda=0.97,
            num_cpu=1,
            sample_mode='trajectories',
            num_traj=40,      # samples = 40*25 = 1000
            save_freq=5,
            evaluation_rollouts=10,
            plot_keys=['stoc_pol_mean', 'running_score', 'eval_score'])
print("time taken = %f" % (timer.time()-ts))
