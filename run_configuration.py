CONF_Main = {
    "environment": "lux_gym:lux-v0",
    "setup": "imitate",
    "model_name": "actor_critic_efficient_six_actions",
    "n_points": 40,  # check tfrecords reading transformation merge_rl
}

CONF_Scrape = {
    "lux_version": "3.1.0",
    "scrape_type": "single",
    "parallel_calls": 8,
    "is_for_rl": True,
    "is_pg_rl": True,
    "team_name": None,  # "Toad Brigade",
    "only_wins": False,
    "only_top_teams": False,
}

CONF_Collect = {
    "is_for_imitator": False,
    "is_for_rl": True,
    "is_pg_rl": True,
    "only_wins": False,
}

CONF_Evaluate = {
    "eval_compare_agent": "compare_agent_sub13",
}

CONF_Imitate = {
    "batch_size": 150,
    "self_imitation": False,
    "with_evaluation": True,
}

CONF_RL = {
    "rl_type": "single_ac_mc",
    # "lambda": 0.8,
    "debug": True,
    "default_lr": 1e-3,
    "batch_size": 500,
    # "iterations_number": 1000,
    # "save_interval": 100,
    "entropy_c": 1.e-4,
    "entropy_c_decay": 0.3,
}
