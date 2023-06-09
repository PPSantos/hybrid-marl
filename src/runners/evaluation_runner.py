from envs import REGISTRY as env_REGISTRY
from functools import partial
from components.episode_buffer import EpisodeBatch
import numpy as np

class EvalRunner:

    def __init__(self, args, logger):
        self.args = args
        self.logger = logger
        self.batch_size = self.args.batch_size_run
        assert self.batch_size == 1

        self.env = env_REGISTRY[self.args.env](**self.args.env_args)
        self.episode_limit = self.env.episode_limit
        self.t = 0

        self.t_env = 0

        self.train_returns = []
        self.test_returns = []
        self.test_returns_comm_p = {}
        self.train_stats = {}
        self.test_stats = {}

        # Log the first run
        self.log_train_stats_t = -1000000

    def setup(self, scheme, groups, preprocess, mac, perception_model=None, rl_scheme=None):
        # Batch to store in replay buffer.
        self.new_batch = partial(EpisodeBatch, scheme, groups, self.batch_size, self.episode_limit + 1,
                                 preprocess=preprocess, device=self.args.device)
        # Batch containing data for action selection.
        self.new_action_selection_batch = partial(EpisodeBatch, rl_scheme, groups, self.batch_size,
                            self.episode_limit + 1, preprocess=preprocess, device=self.args.device)
        self.mac = mac
        self.perc_model = perception_model

    def get_env_info(self):
        return self.env.get_env_info()

    def save_replay(self):
        self.env.save_replay()

    def close_env(self):
        self.env.close()

    def reset(self):
        self.batch = self.new_batch()
        self.action_selection_batch = self.new_action_selection_batch()
        self.env.reset()
        self.t = 0

    def run(self, test_mode=False, comm_p=None):
        self.reset()

        if comm_p == "unif_default":
            comm_p_arg = np.random.rand()
        elif comm_p in ["unif_asymmetric", "unif_dynamic"]:
            comm_p_arg = np.random.rand(self.args.n_agents, self.args.n_agents)
            for a in range(self.args.n_agents):
                comm_p_arg[a, a] = 1.0
        else:
            comm_p_arg = comm_p

        terminated = False
        episode_return = 0
        self.mac.init_hidden(batch_size=self.batch_size)
        if self.perc_model:
            self.perc_model.init_perception_model(batch_size=self.batch_size)

        while not terminated:

            pre_transition_data = {
                "state": [self.env.get_state()],
                "avail_actions": [self.env.get_avail_actions()],
                "obs": [self.env.get_obs()]
            }

            if comm_p == "unif_dynamic" and self.t % 5 == 0:
                comm_p_arg = np.random.rand(self.args.n_agents, self.args.n_agents)
                for a in range(self.args.n_agents):
                    comm_p_arg[a, a] = 1.0

            self.batch.update(pre_transition_data, ts=self.t)
            if self.perc_model:
                perc_model_out = self.perc_model.encode(self.batch, t=self.t,
                            test_mode=test_mode, comm_p=comm_p_arg) # [1,num_agents,latent_dim]
                pre_transition_data["obs"] = [[s for s in perc_model_out.cpu().numpy()[0]]]
            self.action_selection_batch.update(pre_transition_data, ts=self.t)

            # Pass the entire batch of experiences up till now to the agents
            # Receive the actions for each agent at this timestep in a batch of size 1
            actions = self.mac.select_actions(self.action_selection_batch, t_ep=self.t,
                                            t_env=self.t_env, test_mode=test_mode)

            reward, terminated, env_info = self.env.step(actions[0])
            episode_return += reward

            post_transition_data = {
                "actions": actions,
                "reward": [(reward,)],
                "terminated": [(terminated != env_info.get("episode_limit", False),)],
            }

            self.batch.update(post_transition_data, ts=self.t)
            self.action_selection_batch.update(post_transition_data, ts=self.t)

            self.t += 1

        last_data = {
            "state": [self.env.get_state()],
            "avail_actions": [self.env.get_avail_actions()],
            "obs": [self.env.get_obs()]
        }
        self.batch.update(last_data, ts=self.t)
        if self.perc_model:
            perc_model_out = self.perc_model.encode(self.batch, t=self.t,
                        test_mode=test_mode, comm_p=comm_p_arg) # [1,num_agents,latent_dim]
            last_data["obs"] = [[s for s in perc_model_out.cpu().numpy()[0]]]
        self.action_selection_batch.update(last_data, ts=self.t)

        # Select actions in the last stored state
        actions = self.mac.select_actions(self.action_selection_batch, t_ep=self.t,
                                            t_env=self.t_env, test_mode=test_mode)
        self.batch.update({"actions": actions}, ts=self.t)
        self.action_selection_batch.update({"actions": actions}, ts=self.t)

        if test_mode:
            if comm_p != None:
                if comm_p not in self.test_returns_comm_p.keys():
                    self.test_returns_comm_p[comm_p] = []
                cur_returns = self.test_returns_comm_p[comm_p]
                if comm_p in ['unif_default', 'unif_asymmetric', 'unif_dynamic']:
                    log_prefix = "test_p={0}_".format(comm_p)
                else:
                    log_prefix = "test_p={0}_".format(round(comm_p,1))
            else:
                cur_returns = self.test_returns
                log_prefix = "test_"
        else:
            cur_returns = self.train_returns
            log_prefix = ""

        cur_stats = self.test_stats if test_mode else self.train_stats
        cur_stats.update({k: cur_stats.get(k, 0) + env_info.get(k, 0) for k in set(cur_stats) | set(env_info)})
        cur_stats["n_episodes"] = 1 + cur_stats.get("n_episodes", 0)
        cur_stats["ep_length"] = self.t + cur_stats.get("ep_length", 0)

        if not test_mode:
            self.t_env += self.t

        cur_returns.append(episode_return)

        if test_mode and (comm_p == None) and \
        (len(self.test_returns) == self.args.test_nepisode):
            self._log(cur_returns, cur_stats, log_prefix)
        elif test_mode and (comm_p != None) and \
        (len(self.test_returns_comm_p[comm_p]) == self.args.test_nepisode):
            self._log(cur_returns, cur_stats, log_prefix)
        elif self.t_env - self.log_train_stats_t >= self.args.runner_log_interval:
            self._log(cur_returns, cur_stats, log_prefix)
            if hasattr(self.mac.action_selector, "epsilon"):
                self.logger.log_stat("epsilon", self.mac.action_selector.epsilon, self.t_env)
            self.log_train_stats_t = self.t_env

        return self.batch, self.test_returns, self.test_returns_comm_p

    def _log(self, returns, stats, prefix):
        self.logger.log_stat(prefix + "return_mean", np.mean(returns), self.t_env)
        self.logger.log_stat(prefix + "return_std", np.std(returns), self.t_env)
        # returns.clear()

        for k, v in stats.items():
            if k != "n_episodes":
                self.logger.log_stat(prefix + k + "_mean" , v/stats["n_episodes"], self.t_env)
        stats.clear()
