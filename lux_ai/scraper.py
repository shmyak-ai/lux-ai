import abc
import glob
import json
import random
import pathlib

import tensorflow as tf
import gym
# import reverb

from lux_ai import tools, tfrecords_storage
# from lux_ai.dm_reverb_storage import send_data
from lux_gym.envs.lux.action_vectors_new import worker_action_vector, cart_action_vector
from lux_gym.envs.lux.action_vectors_new import empty_worker_action_vectors, empty_cart_action_vectors
from lux_gym.envs.lux.action_vectors_new import dir_action_vector, res_action_vector
from lux_gym.envs.lux.action_vectors_new import action_vector_ct
import lux_gym.envs.tools as env_tools

physical_devices = tf.config.list_physical_devices('GPU')
if len(physical_devices) > 0:
    tf.config.experimental.set_memory_growth(physical_devices[0], True)


REWARD_CAP = 2000000


def scrape(env_name, data, team_name=None, only_wins=False):
    """
    Collects trajectories from an episode to the buffer.

    A buffer contains items, each item consists of several n_points;
    One n_point contains (action, action_probs, action_mask, observation,
                          total reward, temporal_mask, progress);
    action is a response for the current observation,
    reward, done are for the current observation.
    """
    if team_name:
        if data["info"]["TeamNames"][0] == data["info"]["TeamNames"][1] == team_name:
            team_of_interest = -1
        elif data["info"]["TeamNames"][0] == team_name:
            team_of_interest = 1
        elif data["info"]["TeamNames"][1] == team_name:
            team_of_interest = 2
        else:
            return (None, None), (None, None), None
    else:
        team_of_interest = -1

    reward1, reward2 = data["rewards"][0], data["rewards"][1]
    if reward1 is None:
        reward1 = -1
    if reward2 is None:
        reward2 = -1
    final_reward_1 = reward1 / REWARD_CAP if reward1 != -1 else 0
    final_reward_1 = 2 * final_reward_1 - 1
    final_reward_2 = reward2 / REWARD_CAP if reward2 != -1 else 0
    final_reward_2 = 2 * final_reward_2 - 1
    # if reward1 > reward2:
    #     final_reward_1 = tf.constant(1, dtype=tf.float16)
    #     final_reward_2 = tf.constant(-1, dtype=tf.float16)
    # elif reward1 < reward2:
    #     final_reward_2 = tf.constant(1, dtype=tf.float16)
    #     final_reward_1 = tf.constant(-1, dtype=tf.float16)
    # else:
    #     final_reward_1 = final_reward_2 = tf.constant(0, dtype=tf.float16)

    if only_wins:
        if reward1 > reward2:
            team_of_interest = 1
        elif reward1 < reward2:
            team_of_interest = 2
        else:
            team_of_interest = -1

    player1_data = {}
    player2_data = {}

    environment = gym.make(env_name, seed=data["configuration"]["seed"])
    observations, proc_obsns = environment.reset_process()
    configuration = environment.configuration
    current_game_states = environment.game_states
    width, height = current_game_states[0].map.width, current_game_states[0].map.height
    shift = int((32 - width) / 2)  # to make all feature maps 32x32

    def check_actions(actions, player_units_dict, player_cts_list):
        new_actions = []
        for action in actions:
            action_list = action.split(" ")
            if action_list[0] not in ["r", "bw", "bc"]:
                if action_list[1] in player_units_dict.keys():
                    new_actions.append(action)
            else:
                x, y = action_list[1], action_list[2]
                if [int(x), int(y)] in player_cts_list:
                    new_actions.append(action)
        return new_actions

    def update_units_actions(actions, player_units_dict):
        units_with_actions = []
        for action in actions:
            action_list = action.split(" ")
            if action_list[0] not in ["r", "bw", "bc"]:
                units_with_actions.append(action_list[1])
        for key in player_units_dict.keys():
            if key not in units_with_actions:
                actions.append(f"m {key} c")
        return actions

    def update_cts_actions(actions, player_cts_list):
        units_with_actions = []
        for action in actions:
            action_list = action.split(" ")
            if action_list[0] in ["r", "bw", "bc"]:
                x, y = int(action_list[1]), int(action_list[2])
                units_with_actions.append([x, y])
        for ct in player_cts_list:
            if ct not in units_with_actions:
                actions.append(f"idle {ct[0]} {ct[1]}")
        return actions

    def get_actions_dict(player_actions, player_units_dict_active, player_units_dict_all):
        actions_dict = {"workers": {}, "carts": {}, "city_tiles": {}}
        for action in player_actions:
            action_list = action.split(" ")
            # units
            action_type = action_list[0]
            if action_type == "m":  # "m {id} {direction}"
                unit_name = action_list[1]  # "{id}"
                direction = action_list[2]
                if direction == "c":
                    action_type = "idle"
                try:
                    unit_type = "w" if player_units_dict_active[unit_name].is_worker() else "c"
                except KeyError:  # it occurs when there is not valid action proposed
                    continue
                if unit_type == "w":
                    action_vectors = empty_worker_action_vectors.copy()
                    action_vectors[0] = worker_action_vector[action_type]
                    if action_type != "idle":
                        action_vectors[1] = dir_action_vector[direction]
                    actions_dict["workers"][unit_name] = action_vectors
                else:
                    action_vectors = empty_cart_action_vectors.copy()
                    action_vectors[0] = cart_action_vector[action_type]
                    if action_type != "idle":
                        action_vectors[1] = dir_action_vector[direction]
                    actions_dict["carts"][unit_name] = action_vectors
            elif action_type == "t":  # "t {id} {dest_id} {resourceType} {amount}"
                unit_name = action_list[1]
                dest_name = action_list[2]
                resource_type = action_list[3]
                try:
                    unit_type = "w" if player_units_dict_active[unit_name].is_worker() else "c"
                except KeyError:  # these is no such active unit to take action
                    continue
                try:
                    direction = player_units_dict_active[unit_name].pos.direction_to(player_units_dict_all[
                                                                                         dest_name].pos)
                except KeyError:  # there is no such destination unit
                    action_type = "idle"
                    direction = None
                if unit_type == "w":
                    action_vectors = empty_worker_action_vectors.copy()
                    action_vectors[0] = worker_action_vector[action_type]
                    if action_type == "t":
                        action_vectors[1] = dir_action_vector[direction]
                        action_vectors[2] = res_action_vector[resource_type]
                    actions_dict["workers"][unit_name] = action_vectors
                else:
                    action_vectors = empty_cart_action_vectors.copy()
                    action_vectors[0] = cart_action_vector[action_type]
                    if action_type == "t":
                        action_vectors[1] = dir_action_vector[direction]
                        action_vectors[2] = res_action_vector[resource_type]
                    actions_dict["carts"][unit_name] = action_vectors
            elif action_type == "bcity":  # "bcity {id}"
                unit_name = action_list[1]
                action_vectors = empty_worker_action_vectors.copy()
                action_vectors[0] = worker_action_vector[action_type]
                actions_dict["workers"][unit_name] = action_vectors
            elif action_type == "p":  # "p {id}"
                action_type = "idle"  # REPLACEMENT
                unit_name = action_list[1]
                action_vectors = empty_worker_action_vectors.copy()
                action_vectors[0] = worker_action_vector[action_type]
                actions_dict["workers"][unit_name] = action_vectors
            # city tiles
            elif action_type == "r":  # "r {pos.x} {pos.y}"
                x, y = int(action_list[1]), int(action_list[2])
                unit_name = f"ct_{y + shift}_{x + shift}"
                action_vector_name = "ct_research"
                actions_dict["city_tiles"][unit_name] = action_vector_ct[action_vector_name]
            elif action_type == "bw":  # "bw {pos.x} {pos.y}"
                x, y = int(action_list[1]), int(action_list[2])
                unit_name = f"ct_{y + shift}_{x + shift}"
                action_vector_name = "ct_build_worker"
                actions_dict["city_tiles"][unit_name] = action_vector_ct[action_vector_name]
            elif action_type == "bc":  # "bc {pos.x} {pos.y}"
                x, y = int(action_list[1]), int(action_list[2])
                unit_name = f"ct_{y + shift}_{x + shift}"
                action_vector_name = "ct_build_cart"
                actions_dict["city_tiles"][unit_name] = action_vector_ct[action_vector_name]
            elif action_type == "idle":  # "idle {pos.x} {pos.y}"
                x, y = int(action_list[1]), int(action_list[2])
                unit_name = f"ct_{y + shift}_{x + shift}"
                action_vector_name = "ct_idle"
                actions_dict["city_tiles"][unit_name] = action_vector_ct[action_vector_name]
            else:
                raise ValueError
        return actions_dict

    step = 0
    for step in range(0, configuration.episodeSteps):
        assert observations[0]["updates"] == observations[1]["updates"] == data["steps"][step][0]["observation"][
            "updates"]
        # get actions from a record, action for the current obs is in the next step of data
        actions_1 = data["steps"][step + 1][0]["action"]
        actions_2 = data["steps"][step + 1][1]["action"]
        if team_of_interest == 1 or team_of_interest == -1:
            # get units to know their types etc.
            player1 = current_game_states[0].players[observations[0].player]
            player1_units_dict_active = {}
            player1_units_dict_all = {}
            for unit in player1.units:
                player1_units_dict_all[unit.id] = unit
                if unit.can_act():
                    player1_units_dict_active[unit.id] = unit
            # get citytiles
            player1_ct_list_active = []
            for city in player1.cities.values():
                for citytile in city.citytiles:
                    if citytile.cooldown < 1:
                        x_coord, y_coord = citytile.pos.x, citytile.pos.y
                        player1_ct_list_active.append([x_coord, y_coord])
            # copy actions since we need preprocess them before recording
            if actions_1 is None:
                actions_1_vec = []
            else:
                actions_1_vec = actions_1.copy()
            # check actions and erase invalid ones
            actions_1_vec = check_actions(actions_1_vec, player1_units_dict_active, player1_ct_list_active)
            # if no action and unit can act, add "m {id} c"
            actions_1_vec = update_units_actions(actions_1_vec, player1_units_dict_active)
            # in no action and ct can act, add "idle {x} {y}"
            # actions_1_vec = update_cts_actions(actions_1_vec, player1_ct_list_active)
            # get actions vector representation
            actions_1_dict = get_actions_dict(actions_1_vec, player1_units_dict_active, player1_units_dict_all)
            # process only workers data
            actions_1_dict.pop("carts")
            actions_1_dict.pop("city_tiles")
            proc_obsns[0].pop("carts")
            proc_obsns[0].pop("city_tiles")
            # probs are similar to actions
            player1_data = tools.add_point(player1_data, actions_1_dict, actions_1_dict, proc_obsns[0], step)
        if team_of_interest == 2 or team_of_interest == -1:
            # get units to know their types etc.
            player2 = current_game_states[1].players[(observations[0].player + 1) % 2]
            player2_units_dict_active = {}
            player2_units_dict_all = {}
            for unit in player2.units:
                player2_units_dict_all[unit.id] = unit
                if unit.can_act():
                    player2_units_dict_active[unit.id] = unit
            # get citytiles
            player2_ct_list_active = []
            for city in player2.cities.values():
                for citytile in city.citytiles:
                    if citytile.cooldown < 1:
                        x_coord, y_coord = citytile.pos.x, citytile.pos.y
                        player2_ct_list_active.append([x_coord, y_coord])
            # copy actions since we need preprocess them before recording
            if actions_2 is None:
                actions_2_vec = []
            else:
                actions_2_vec = actions_2.copy()
            # check actions and erase invalid ones
            actions_2_vec = check_actions(actions_2_vec, player2_units_dict_active, player2_ct_list_active)
            # if no action and unit can act, add "m {id} c"
            actions_2_vec = update_units_actions(actions_2_vec, player2_units_dict_active)
            # in no action and ct can act, add "idle {x} {y}"
            # actions_2_vec = update_cts_actions(actions_2_vec, player2_ct_list_active)
            # get actions vector representation
            actions_2_dict = get_actions_dict(actions_2_vec, player2_units_dict_active, player2_units_dict_all)
            # process only workers data
            actions_2_dict.pop("carts")
            actions_2_dict.pop("city_tiles")
            proc_obsns[1].pop("carts")
            proc_obsns[1].pop("city_tiles")
            # probs are similar to actions
            player2_data = tools.add_point(player2_data, actions_2_dict, actions_2_dict, proc_obsns[1], step)

        # dones, observations, proc_obsns = environment.step_process((actions_1, actions_2))
        dones, (obs1, obs2) = environment.step((actions_1, actions_2))
        observations = (obs1, obs2)
        current_game_states = environment.game_states

        first_player_obs, second_player_obs = None, None
        if team_of_interest == 1 or team_of_interest == -1:
            first_player_obs = env_tools.get_separate_outputs(obs1, current_game_states[0])
        if team_of_interest == 2 or team_of_interest == -1:
            second_player_obs = env_tools.get_separate_outputs(obs2, current_game_states[1])
        proc_obsns = (first_player_obs, second_player_obs)

        if any(dones):
            break

    # count_team_1 = 0
    # for value in list(player1_data.values()):
    #     count_team_1 += len(value.data)
    # count_team_2 = 0
    # for value in list(player2_data.values()):
    #     count_team_2 += len(value.data)
    # print(f"Team 1 count: {count_team_1}; Team 2 count: {count_team_2}; Team to add: {team_of_interest}")

    progress = tf.linspace(0., 1., step + 2)[:-1]
    progress = tf.cast(progress, dtype=tf.float16)

    if team_of_interest == -1:
        output = (player1_data, player2_data), (final_reward_1, final_reward_2), progress
    elif team_of_interest == 1:
        output = (player1_data, None), (final_reward_1, None), progress
    elif team_of_interest == 2:
        output = (None, player2_data), (None, final_reward_2), progress
    else:
        raise ValueError

    return output


def scrape_file(env_name, file_name, team_name, lux_version, only_wins,
                feature_maps_shape, acts_shape, record_number, is_for_rl, is_pg_rl):
    with open(file_name, "r") as read_file:
        raw_name = pathlib.Path(file_name).stem
        data = json.load(read_file)
        if data["version"] != lux_version:
            print(f"File {file_name}; is for an inappropriate lux version.")
            return

    output = scrape(env_name, data, team_name, only_wins)
    (player1_data, player2_data), (final_reward_1, final_reward_2), progress = output
    if player1_data == player2_data is None:
        print(f"File {file_name}; does not have a required team.")
        return
    else:
        print(f"File {file_name}; {record_number}; recording.")

    if team_name is None:
        team_names = data['info']['TeamNames']
        reward1, reward2 = data['rewards']
        if reward1 is None:
            reward1 = -1
        if reward2 is None:
            reward2 = -1
        if reward1 > reward2:
            team_name = team_names[0]
        elif reward1 < reward2:
            team_name = team_names[1]
        else:
            team_name = 'Draw'

    tfrecords_storage.record(player1_data, player2_data, final_reward_1, final_reward_2,
                             feature_maps_shape, acts_shape, record_number,
                             raw_name + "_" + team_name, progress,
                             is_for_rl,
                             is_pg_rl=is_pg_rl)


class Agent(abc.ABC):

    def __init__(self, config):  # ,
        # buffer_table_names, buffer_server_port,
        # ray_queue=None, collector_id=None, workers_info=None, num_collectors=None
        # ):
        """
        Args:
            config: A configuration dictionary
            # buffer_table_names: dm reverb server table names
            # buffer_server_port: a port where a dm reverb server was initialized
            # ray_queue: a ray interprocess queue to store neural net weights
            # collector_id: to identify a current collector if there are several ones
            # workers_info: a ray interprocess (remote) object to store shared information
            # num_collectors: a total amount of collectors
        """
        # self._n_players = 2
        self._actions_shape = [item.shape for item in empty_worker_action_vectors]
        self._env_name = config["environment"]

        self._feature_maps_shape = tools.get_feature_maps_shape(config["environment"])

        # self._n_points = config["n_points"]

        # self._table_names = buffer_table_names
        # self._client = reverb.Client(f'localhost:{buffer_server_port}')

        # self._ray_queue = ray_queue
        # self._collector_id = collector_id
        # self._workers_info = workers_info
        # self._num_collectors = num_collectors

        self._is_for_rl = config["is_for_rl"]
        self._is_pg_rl = config["is_pg_rl"]
        self._lux_version = config["lux_version"]
        self._team_name = config["team_name"]
        self._only_wins = config["only_wins"]
        self._only_top_teams = config["only_top_teams"]
        self._top_teams = {'Toad Brigade', 'RL is all you need', 'ironbar', 'Team Durrett', 'A.Saito'}

        self._files = glob.glob("./data/jsons/*.json")
        if self._is_for_rl:
            self._data_path = "./data/tfrecords/rl/storage/"
        else:
            self._data_path = "./data/tfrecords/imitator/train/"
        already_saved_files = glob.glob(self._data_path + "*.tfrec")
        self._saved_submissions = set()
        for file_name in already_saved_files:
            raw_name = pathlib.Path(file_name).stem
            self._saved_submissions.add(raw_name.split("_")[0])

    def _scrape(self, data, team_name=None, only_wins=False):
        output = scrape(self._env_name, data, team_name, only_wins)
        return output

    # def _send_data_to_dmreverb_buffer(self, players_data, rewards, progress):
    #     player1_data, player2_data = players_data
    #     final_reward_1, final_reward_2 = rewards
    #     arguments_1 = (player1_data, final_reward_1, progress,
    #                    self._feature_maps_shape, self._actions_number, self._n_points,
    #                    self._client, self._table_names)
    #     arguments_2 = (player2_data, final_reward_2, progress,
    #                    self._feature_maps_shape, self._actions_number, self._n_points,
    #                    self._client, self._table_names)
    #     if self._team_of_interest == -1:
    #         send_data(*arguments_1)
    #         send_data(*arguments_2)
    #     elif self._team_of_interest == 1:
    #         send_data(*arguments_1)
    #     elif self._team_of_interest == 2:
    #         send_data(*arguments_2)

    def scrape_once(self):
        file_name = random.sample(self._files, 1)[0]
        with open(file_name, "r") as read_file:
            data = json.load(read_file)
        self._scrape(data)

    def scrape_all(self, files_to_save=3):
        j = 0
        for i, file_name in enumerate(self._files):
            with open(file_name, "r") as read_file:
                raw_name = pathlib.Path(file_name).stem
                # if f"{self._data_path}{raw_name}_{self._team_name}.tfrec" in self._already_saved_files:
                if raw_name in self._saved_submissions:
                    print(f"File {file_name} for {self._team_name}; {i}; is already saved.")
                    # data = json.load(read_file)
                    # print(f"Team 0: {data['info']['TeamNames'][0]}, Team 1: {data['info']['TeamNames'][1]}")
                    continue
                data = json.load(read_file)
                if data["version"] != self._lux_version:
                    print(f"File {file_name}; {i}; is for an inappropriate lux version.")
                    continue

            if self._only_top_teams:
                team_names = set(data['info']['TeamNames'])
                if not team_names.issubset(self._top_teams):
                    continue

            (player1_data, player2_data), (final_reward_1, final_reward_2), progress = self._scrape(data,
                                                                                                    self._team_name,
                                                                                                    self._only_wins)
            if player1_data == player2_data is None:
                print(f"File {file_name}; {i}; does not have a required team.")
                continue
            else:
                print(f"File {file_name}; {i}; recording.")

            if self._team_name:
                team_name = self._team_name
            else:
                team_names = data['info']['TeamNames']
                reward1, reward2 = data['rewards']
                if reward1 is None:
                    reward1 = -1
                if reward2 is None:
                    reward2 = -1
                if reward1 > reward2:
                    team_name = team_names[0]
                elif reward1 < reward2:
                    team_name = team_names[1]
                else:
                    team_name = 'Draw'

            tfrecords_storage.record(player1_data, player2_data, final_reward_1, final_reward_2,
                                     self._feature_maps_shape, self._actions_shape, i,
                                     raw_name + "_" + team_name, progress,
                                     self._is_for_rl,
                                     is_pg_rl=self._is_pg_rl)
            j += 1
            if j == files_to_save:
                print(f"{files_to_save} files saved, exit.")
                return
