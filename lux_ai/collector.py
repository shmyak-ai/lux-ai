def collect(config_out, input_data_out, data_path_out, collector_n_out, global_var_actor_out=None,
            steps=10):
    import abc
    import time
    from multiprocessing import Process

    import tensorflow as tf
    import gym
    # import reverb
    # import ray

    import lux_gym.agents.agents as agents
    from lux_ai import tools, tfrecords_storage
    from lux_gym.envs.lux.action_vectors_new import empty_worker_action_vectors

    physical_devices = tf.config.list_physical_devices('GPU')
    if len(physical_devices) > 0:
        tf.config.experimental.set_memory_growth(physical_devices[0], True)

    # REWARD_CAP = 2000000

    class Agent(abc.ABC):
        def __init__(self, config, data):
            # buffer_table_names, buffer_server_port,
            # ray_queue=None, collector_id=None, workers_info=None, num_collectors=None
            # ):
            """
            Args:
                config: A configuration dictionary
                data: a neural net weights
                # buffer_table_names: dm reverb server table names
                # buffer_server_port: a port where a dm reverb server was initialized
                # ray_queue: a ray interprocess queue to store neural net weights
                # collector_id: to identify a current collector if there are several ones
                # workers_info: a ray interprocess (remote) object to store shared information
                # num_collectors: a total amount of collectors
            """
            self._env_name = config["environment"]
            self._n_points = config["n_points"]
            self._model_name = config["model_name"]
            if data is None:
                print("Collecting from a random agent.")
            self._agent = agents.get_processing_agent(self._model_name, data)

            self._feature_maps_shape = tools.get_feature_maps_shape(config["environment"])
            self._actions_shape = [item.shape for item in empty_worker_action_vectors]

            # self._table_names = buffer_table_names
            # self._client = reverb.Client(f'localhost:{buffer_server_port}')
            # self._ray_queue = ray_queue
            # self._collector_id = collector_id
            # self._workers_info = workers_info
            # self._num_collectors = num_collectors
            self._only_wins = config["only_wins"]
            self._is_for_rl = config["is_for_rl"]
            self._is_pg_rl = config["is_pg_rl"]

        def _collect(self, agent):
            """
            Collects trajectories from an episode. Episodes consists of n_points.

            One n_point contains (action, action_probs, action_mask, observation,
                                  total reward, temporal_mask, progress);
            action is a response for the current observation,
            reward, done are for the current observation.
            """

            player1_data = {}
            player2_data = {}

            environment = gym.make(self._env_name)
            observations = environment.reset()
            configuration = environment.configuration
            game_states = environment.game_states
            actions_1, actions_1_dict, actions_1_probs, proc_obs1, reward1 = agent(observations[0],
                                                                                   configuration, game_states[0])
            actions_2, actions_2_dict, actions_2_probs, proc_obs2, reward2 = agent(observations[1],
                                                                                   configuration, game_states[1])

            step = 0
            player1_data = tools.add_point(player1_data, actions_1_dict, actions_1_probs, proc_obs1, step)
            player2_data = tools.add_point(player2_data, actions_2_dict, actions_2_probs, proc_obs2, step)

            #  for step in range(1, configuration.episodeSteps):
            while True:
                step += 1
                player1_prev_alive_units = [item.id for item in game_states[0].player.units]
                player2_prev_alive_units = [item.id for item in game_states[1].player.units]
                dones, observations = environment.step((actions_1, actions_2))
                game_states = environment.game_states
                if any(dones):
                    player1_alive_units_ids = []
                    player1_died_on_last_step_units_ids = []
                    player1_alive_units = [item.id for item in game_states[0].player.units]
                    for unit in player1_prev_alive_units:
                        if unit in player1_alive_units:
                            player1_alive_units_ids.append(unit)
                        else:
                            player1_died_on_last_step_units_ids.append(unit)

                    player2_alive_units_ids = []
                    player2_died_on_last_step_units_ids = []
                    player2_alive_units = [item.id for item in game_states[1].player.units]
                    for unit in player2_prev_alive_units:
                        if unit in player2_alive_units:
                            player2_alive_units_ids.append(unit)
                        else:
                            player2_died_on_last_step_units_ids.append(unit)

                    break
                actions_1, actions_1_dict, actions_1_probs, proc_obs1, reward1 = agent(observations[0],
                                                                                       configuration, game_states[0])
                actions_2, actions_2_dict, actions_2_probs, proc_obs2, reward2 = agent(observations[1],
                                                                                       configuration, game_states[1])

                player1_data = tools.add_point(player1_data, actions_1_dict, actions_1_probs, proc_obs1, step)
                player2_data = tools.add_point(player2_data, actions_2_dict, actions_2_probs, proc_obs2, step)

            reward1 = observations[0]["reward"]
            reward2 = observations[1]["reward"]
            unit_rewards = {}
            if reward1 != reward2:
                if reward1 > reward2:  # 1 player won
                    win_data = player1_data
                    win_died_on_last_step_units_ids = player1_died_on_last_step_units_ids
                    # win_alive_units_ids = player1_alive_units_ids
                    lose_data = player2_data
                    lose_died_on_last_step_units_ids = player2_died_on_last_step_units_ids
                    lose_alive_units_ids = player2_alive_units_ids
                else:
                    win_data = player2_data
                    win_died_on_last_step_units_ids = player2_died_on_last_step_units_ids
                    # win_alive_units_ids = player2_alive_units_ids
                    lose_data = player1_data
                    lose_died_on_last_step_units_ids = player1_died_on_last_step_units_ids
                    lose_alive_units_ids = player1_alive_units_ids

                for unit_id in win_data.keys():
                    if unit_id in win_died_on_last_step_units_ids:
                        unit_rewards[unit_id] = tf.constant(-0.33, dtype=tf.float16)
                    else:
                        unit_rewards[unit_id] = tf.constant(1, dtype=tf.float16)
                for unit_id in lose_data.keys():
                    if unit_id in lose_died_on_last_step_units_ids:
                        unit_rewards[unit_id] = tf.constant(-1, dtype=tf.float16)
                    elif unit_id in lose_alive_units_ids:
                        unit_rewards[unit_id] = tf.constant(0.33, dtype=tf.float16)
                    else:
                        unit_rewards[unit_id] = tf.constant(0, dtype=tf.float16)
            else:
                players_data = {**player1_data, **player2_data}
                for unit_id in players_data.keys():
                    if unit_id in player1_died_on_last_step_units_ids + player2_died_on_last_step_units_ids:
                        unit_rewards[unit_id] = tf.constant(-1, dtype=tf.float16)
                    else:
                        unit_rewards[unit_id] = tf.constant(0.33, dtype=tf.float16)

            progress = tf.linspace(0., 1., step + 2)[:-1]
            progress = tf.cast(progress, dtype=tf.float16)

            # if reward1 > reward2:
            #     final_reward_1 = tf.constant(1, dtype=tf.float16)
            #     final_reward_2 = tf.constant(-1, dtype=tf.float16)
            # elif reward1 < reward2:
            #     final_reward_2 = tf.constant(1, dtype=tf.float16)
            #     final_reward_1 = tf.constant(-1, dtype=tf.float16)
            # else:
            #     final_reward_1 = final_reward_2 = tf.constant(0, dtype=tf.float16)

            # final_reward_1 = reward1 / REWARD_CAP if reward1 != -1 else 0
            # final_reward_1 = 2 * final_reward_1 - 1
            # final_reward_2 = reward2 / REWARD_CAP if reward2 != -1 else 0
            # final_reward_2 = 2 * final_reward_2 - 1

            if self._only_wins:
                if reward1 > reward2:
                    output = (player1_data, None), unit_rewards, progress
                elif reward1 < reward2:
                    output = (None, player2_data), unit_rewards, progress
                else:
                    output = (player1_data, player2_data), unit_rewards, progress
            else:
                output = (player1_data, player2_data), unit_rewards, progress

            return output

        def collect_once(self):
            return self._collect(self._agent)

        def collect_and_store(self, collect_n, data_path, collector_n):
            (player1_data, player2_data), rewards, progress = self.collect_once()
            tfrecords_storage.record(player1_data, player2_data, rewards,
                                     self._feature_maps_shape, self._actions_shape, collect_n,
                                     collect_n, progress,
                                     is_for_rl=self._is_for_rl, save_path=data_path, collector_n=collector_n,
                                     is_pg_rl=self._is_pg_rl)

    def collect_and_store(iteration, conf, in_data, data_path, collector_n):
        collect_agent = Agent(conf, in_data)
        collect_agent.collect_and_store(iteration, data_path, collector_n)

    # collect_and_store(0, config_out, input_data_out, data_path_out, collector_n_out)

    for i in range(steps):
        p = Process(target=collect_and_store, args=(i, config_out, input_data_out, data_path_out, collector_n_out))
        p.start()
        p.join()

    # if global_var_actor_out is not None:
    #     ray.get(global_var_actor_out.set_done.remote(True))

    print("Collecting is done.")
    time.sleep(1)
