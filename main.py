import pickle
import os
import glob
import random
import pathlib
import time
import collections
import itertools

import numpy as np
import ray

from lux_ai import scraper, collector, evaluator, imitator, trainer_ac, trainer_pg, trainer_ac_mc, tools
from lux_gym.envs.lux.action_vectors_new import empty_worker_action_vectors
from run_configuration import CONF_Scrape, CONF_Collect, CONF_RL, CONF_Main, CONF_Imitate, CONF_Evaluate


def scrape():

    config = {**CONF_Main, **CONF_Scrape}

    if config["scrape_type"] == "single":
        scraper_agent = scraper.Agent(config)
        scraper_agent.scrape_all()
    elif config["scrape_type"] == "multi":
        parallel_calls = config["parallel_calls"]
        actions_shape = [item.shape for item in empty_worker_action_vectors]
        env_name = config["environment"]
        feature_maps_shape = tools.get_feature_maps_shape(config["environment"])
        lux_version = config["lux_version"]
        team_name = config["team_name"]
        only_wins = config["only_wins"]
        is_for_rl = config["is_for_rl"]
        is_pg_rl = config["is_pg_rl"]
        files_pool = set(glob.glob("./data/jsons/*.json"))
        if is_for_rl:
            data_path = "./data/tfrecords/rl/storage/"
        else:
            data_path = "./data/tfrecords/imitator/train/"
        already_saved_files = glob.glob(data_path + "*.tfrec")
        saved_submissions = set()
        for file_name in already_saved_files:
            raw_name = pathlib.Path(file_name).stem
            saved_submissions.add(raw_name.split("_")[0])

        file_names_saved = []
        for i, file_name in enumerate(files_pool):
            raw_name = pathlib.Path(file_name).stem
            if raw_name in saved_submissions:
                print(f"File {file_name} for {team_name}; is already saved.")
                file_names_saved.append(file_name)
        files_pool -= set(file_names_saved)

        set_size = int(len(files_pool) / parallel_calls)
        sets = []
        for _ in range(parallel_calls):
            new_set = set(random.sample(files_pool, set_size))
            files_pool -= new_set
            sets.append(new_set)

        for i, file_names in enumerate(zip(*sets)):
            print(f"Iteration {i} starts")
            ray.init(num_cpus=parallel_calls, include_dashboard=False)
            scraper_object = ray.remote(scraper.scrape_file)
            futures = [scraper_object.remote(env_name, file_names[j], team_name, lux_version, only_wins,
                                             feature_maps_shape, actions_shape, i, is_for_rl, is_pg_rl)
                       for j in range(len(file_names))]
            _ = ray.get(futures)
            ray.shutdown()
    else:
        raise ValueError

    return 0


def collect(input_data):
    config = {**CONF_Main, **CONF_Collect}
    if config["is_for_imitator"]:
        data_path = "data/tfrecords/imitator/storage_0/"
    elif config["is_for_rl"]:
        data_path = "data/tfrecords/rl/storage_0/"
    else:
        raise NotImplementedError

    # collector.collect(config, input_data, data_path, 9)

    ray.init(include_dashboard=False)
    collector_object = ray.remote(collector.collect)
    futures = [collector_object.remote(config, input_data, data_path, j, steps=10) for j in range(2)]
    _ = ray.get(futures)
    ray.shutdown()


def evaluate(input_data):
    config = {**CONF_Main, **CONF_Evaluate}
    eval_agent = evaluator.Agent(config, input_data)
    eval_agent.evaluate()


def imitate(input_data):
    config = {**CONF_Main, **CONF_Imitate, **CONF_Evaluate, **CONF_Collect}

    if config["self_imitation"]:
        prev_n = 2
        for i in range(10):
            print(f"Self imitation, cycle {i}.")
            current_n = i % 3  # current and prev to use
            next_n = (i + 1) % 3  # next to collect
            data_path = f"data/tfrecords/imitator/storage_{next_n}/"
            fnames_train = glob.glob("data/tfrecords/imitator/train/*.tfrec")
            fnames_curr = glob.glob(f"data/tfrecords/imitator/storage_{current_n}/*.tfrec")
            fnames_prev = glob.glob(f"data/tfrecords/imitator/storage_{prev_n}/*.tfrec")
            self_exp_n = len(fnames_curr) + len(fnames_prev)
            fnames_train = random.choices(fnames_train, k=self_exp_n)
            filenames = fnames_train + fnames_prev + fnames_curr

            files = glob.glob("./data/weights/*.pickle")
            if len(files) > 0:
                with open(files[-1], 'rb') as datafile:
                    input_data = pickle.load(datafile)
                    raw_name = pathlib.Path(files[-1]).stem
                    print(f"Training and collecting from {raw_name}.pickle weights.")

            # trainer_agent = imitator.Agent(config, input_data, filenames=filenames, current_cycle=i)
            # trainer_agent.self_imitate()

            ray.init(num_gpus=1, include_dashboard=False)
            # remote objects creation
            trainer_object = ray.remote(num_gpus=1)(imitator.Agent)
            eval_object = ray.remote(evaluator.Agent)
            collector_object = ray.remote(collector.collect)
            # initialization
            workers_info = tools.GlobalVarActor.remote()
            imitator_agent = trainer_object.remote(config, input_data, workers_info, filenames, i)
            eval_agent = eval_object.remote(config, input_data, workers_info)
            # remote call
            trainer_future = imitator_agent.self_imitate.remote()
            eval_future = eval_agent.evaluate.remote()
            col_futures = [collector_object.remote(config, input_data, data_path, j, global_var_actor_out=workers_info)
                           for j in range(2)]
            # getting results from remote functions
            _ = ray.get(trainer_future)
            _ = ray.get(eval_future)
            _ = ray.get(col_futures)
            time.sleep(1)
            ray.shutdown()
            prev_n = current_n
            time.sleep(5)
    elif config["with_evaluation"]:
        ray.init(num_gpus=1, include_dashboard=False)
        # remote objects creation
        trainer_object = ray.remote(num_gpus=1)(imitator.Agent)
        eval_object = ray.remote(evaluator.Agent)
        # initialization
        workers_info = tools.GlobalVarActor.remote()
        trainer_agent = trainer_object.remote(config, input_data, workers_info)
        eval_agent = eval_object.remote(config, input_data, workers_info)
        # remote call
        trainer_future = trainer_agent.imitate.remote()
        eval_future = eval_agent.evaluate.remote()
        # getting results from remote functions
        _ = ray.get(trainer_future)
        _ = ray.get(eval_future)
        time.sleep(1)
        ray.shutdown()
    else:
        trainer_agent = imitator.Agent(config, input_data)
        trainer_agent.imitate()


def rl_train(input_data):  # , checkpoint):
    config = {**CONF_Main, **CONF_RL, **CONF_Evaluate, **CONF_Collect}
    # if checkpoint is not None:
    #     path = str(Path(checkpoint).parent)  # due to https://github.com/deepmind/reverb/issues/12
    #     checkpointer = reverb.checkpointers.DefaultCheckpointer(path=path)
    # else:
    #     checkpointer = None
    # feature_maps_shape = tools.get_feature_maps_shape(config["environment"])
    # buffer = storage.UniformBuffer(feature_maps_shape,
    #                                num_tables=1, min_size=config["batch_size"], max_size=config["buffer_size"],
    #                                n_points=config["n_points"], checkpointer=checkpointer)
    if config["rl_type"] == "single":
        trainer_ac.ac_agent_run(config, input_data)
    elif config["rl_type"] == "single_pg":
        trainer_pg.pg_agent_run(config, input_data)
    elif config["rl_type"] == "single_ac_mc":
        # data_list = glob.glob(f"data/tfrecords/rl/storage/*.tfrec")
        data_list = [glob.glob(f"data/tfrecords/rl/storage_{i}/*.tfrec") for i in [j for j in range(10)]]
        data_list = list(itertools.chain.from_iterable(data_list))
        trainer_ac_mc.ac_mc_agent_run(config, input_data, filenames_in=data_list)
    elif config["rl_type"] == "with_evaluation":
        for i in range(10):
            print(f"RL learning, cycle {i}.")
            ray.init(num_gpus=1, include_dashboard=False)
            # remote objects creation
            trainer_object = ray.remote(num_gpus=1)(trainer_ac.ac_agent_run)
            eval_object = ray.remote(evaluator.Agent)
            # initialization
            workers_info = tools.GlobalVarActor.remote()
            eval_agent = eval_object.remote(config, input_data, workers_info)
            # remote call
            trainer_future = trainer_object.remote(config, input_data, i, workers_info)
            eval_future = eval_agent.evaluate.remote()
            # getting results from remote functions
            _ = ray.get(trainer_future)
            _ = ray.get(eval_future)
            time.sleep(1)
            ray.shutdown()
            time.sleep(5)
    elif config["rl_type"] == "continuous_pg":
        prev_n = 4
        prev_prev_n = 3
        prev_prev_prev_n = 2
        for i in range(10):
            print(f"PG learning, cycle {i}.")
            current_n = i % 5  # current and prev to use
            next_n = (i + 1) % 5  # next to collect
            data_path = f"data/tfrecords/rl/storage_{next_n}/"  # path to save in
            fnames_fixed = glob.glob("data/tfrecords/rl/storage/*.tfrec")
            fnames_curr = glob.glob(f"data/tfrecords/rl/storage_{current_n}/*.tfrec")
            fnames_prev = glob.glob(f"data/tfrecords/rl/storage_{prev_n}/*.tfrec")
            fnames_prev_prev = glob.glob(f"data/tfrecords/rl/storage_{prev_prev_n}/*.tfrec")
            fnames_prev_prev_prev = glob.glob(f"data/tfrecords/rl/storage_{prev_prev_prev_n}/*.tfrec")
            self_exp_n = len(fnames_curr) + len(fnames_prev) + len(fnames_prev_prev) + len(fnames_prev_prev_prev)
            fnames_fixed = random.choices(fnames_fixed, k=self_exp_n)
            filenames = fnames_fixed + fnames_prev + fnames_prev_prev + fnames_prev_prev_prev + fnames_curr

            files = glob.glob("./data/weights/*.pickle")
            if len(files) > 0:
                with open(files[-1], 'rb') as datafile:
                    input_data = pickle.load(datafile)
                    raw_name = pathlib.Path(files[-1]).stem
                    print(f"Training and collecting from {raw_name}.pickle weights.")

            # trainer_pg.pg_agent_run(config, input_data, None, filenames, 0)

            ray.init(num_gpus=1, include_dashboard=False)
            # remote objects creation
            trainer_object = ray.remote(num_gpus=1)(trainer_pg.pg_agent_run)
            eval_object = ray.remote(evaluator.Agent)
            collector_object = ray.remote(collector.collect)
            # initialization
            workers_info = tools.GlobalVarActor.remote()
            eval_agent = eval_object.remote(config, input_data, workers_info)
            # remote call
            trainer_future = trainer_object.remote(config, input_data, workers_info, filenames, i)
            eval_future = eval_agent.evaluate.remote()
            col_futures = [collector_object.remote(config, input_data, data_path, j, global_var_actor_out=workers_info)
                           for j in range(2)]
            # getting results from remote functions
            _ = ray.get(trainer_future)
            _ = ray.get(eval_future)
            _ = ray.get(col_futures)
            time.sleep(1)
            ray.shutdown()

            prev_prev_prev_n = prev_prev_n
            prev_prev_n = prev_n
            prev_n = current_n
            time.sleep(5)
    elif config["rl_type"] == "from_scratch_pg":
        amount_of_pieces = 20
        previous_pieces = collections.deque([i + 2 for i in range(amount_of_pieces - 2)])
        for i in range(100):
            print(f"PG learning, cycle {i}.")
            current_n = i % amount_of_pieces  # current and prev to use
            next_n = (i + 1) % amount_of_pieces  # next to collect
            data_path = f"data/tfrecords/rl/storage_{next_n}/"  # path to save in
            fnames_curr = glob.glob(f"data/tfrecords/rl/storage_{current_n}/*.tfrec")
            fnames_prev_list = [glob.glob(f"data/tfrecords/rl/storage_{i}/*.tfrec") for i in previous_pieces]
            fnames_prev_list = list(itertools.chain.from_iterable(fnames_prev_list))
            filenames = fnames_prev_list + fnames_curr

            files = glob.glob("./data/weights/*.pickle")
            if len(files) > 0:
                with open(files[-1], 'rb') as datafile:
                    input_data = pickle.load(datafile)
                    raw_name = pathlib.Path(files[-1]).stem
                    print(f"Training and collecting from {raw_name}.pickle weights.")

            # trainer_pg.pg_agent_run(config, input_data, None, filenames, 0)

            ray.init(num_gpus=1, include_dashboard=False)
            # remote objects creation
            trainer_object = ray.remote(num_gpus=1)(trainer_pg.pg_agent_run)
            collector_object = ray.remote(collector.collect)
            # initialization
            workers_info = tools.GlobalVarActor.remote()
            # remote call
            trainer_future = trainer_object.remote(config, input_data, workers_info, filenames, i)
            col_futures = [
                collector_object.remote(config, input_data, data_path, j, global_var_actor_out=workers_info)
                for j in range(2)]
            # getting results from remote functions
            _ = ray.get(trainer_future)
            _ = ray.get(col_futures)
            time.sleep(1)
            ray.shutdown()

            previous_pieces.rotate(-1)
            previous_pieces[-1] = current_n
            time.sleep(5)
    elif config["rl_type"] == "continuous_ac_mc":
        amount_of_pieces = 10
        previous_pieces = collections.deque([i + 2 for i in range(amount_of_pieces - 2)])
        for i in range(100):
            print(f"PG learning, cycle {i}.")
            current_n = i % amount_of_pieces  # current and prev to use
            next_n = (i + 1) % amount_of_pieces  # next to collect
            data_path = f"data/tfrecords/rl/storage_{next_n}/"  # path to save in
            files_to_delete = glob.glob(data_path + "*")
            for f in files_to_delete:
                os.remove(f)
            fnames_fixed = glob.glob("data/tfrecords/rl/storage/*.tfrec")
            fnames_curr = glob.glob(f"data/tfrecords/rl/storage_{current_n}/*.tfrec")
            fnames_prev_list = [glob.glob(f"data/tfrecords/rl/storage_{i}/*.tfrec") for i in previous_pieces]
            fnames_prev_list = list(itertools.chain.from_iterable(fnames_prev_list))
            self_exp_n = len(fnames_curr) + len(fnames_prev_list)
            n_fixed = min(int(self_exp_n / 2), len(fnames_fixed))
            fnames_fixed = random.choices(fnames_fixed, k=n_fixed)
            filenames = fnames_fixed + fnames_prev_list + fnames_curr

            files = glob.glob("./data/weights/*.pickle")
            if len(files) > 0:
                raw_names = [int(pathlib.Path(file_name).stem) for file_name in files]
                np_names = np.array(raw_names)
                last_file_arg_number = np.argmax(np_names)
                last_file = files[last_file_arg_number]
                with open(last_file, 'rb') as datafile:
                    input_data = pickle.load(datafile)
                    raw_name = raw_names[last_file_arg_number]
                    print(f"Training and collecting from {raw_name}.pickle weights.")

            ray.init(num_gpus=1, include_dashboard=False)
            # remote objects creation
            trainer_object = ray.remote(num_gpus=1)(trainer_ac_mc.ac_mc_agent_run)
            eval_object = ray.remote(evaluator.Agent)
            collector_object = ray.remote(collector.collect)
            # initialization
            workers_info = tools.GlobalVarActor.remote()
            eval_agent = eval_object.remote(config, input_data, workers_info)
            # remote call
            trainer_future = trainer_object.remote(config, input_data, workers_info, filenames, i)
            eval_future = eval_agent.evaluate.remote()
            col_futures = [
                collector_object.remote(config, input_data, data_path, j, global_var_actor_out=workers_info)
                for j in range(2)]
            # getting results from remote functions
            _ = ray.get(trainer_future)
            _ = ray.get(eval_future)
            _ = ray.get(col_futures)
            time.sleep(1)
            ray.shutdown()

            previous_pieces.rotate(-1)
            previous_pieces[-1] = current_n
            time.sleep(5)
    else:
        raise NotImplementedError


if __name__ == '__main__':
    try:
        with open('data/data.pickle', 'rb') as file:
            init_data = pickle.load(file)
    except FileNotFoundError:
        init_data = None

    if CONF_Main["setup"] == "scrape":
        scrape()
    elif CONF_Main["setup"] == "collect":
        collect(init_data)
    elif CONF_Main["setup"] == "evaluate":
        evaluate(init_data)
    elif CONF_Main["setup"] == "imitate":
        imitate(init_data)
    elif CONF_Main["setup"] == "rl":
        rl_train(init_data)
    else:
        raise NotImplementedError
