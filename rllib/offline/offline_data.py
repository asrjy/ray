import logging
import numpy as np
from pathlib import Path
import random
import ray
from ray.actor import ActorHandle
from typing import Any, Dict, List, Optional, Union

from ray.rllib.algorithms.algorithm_config import AlgorithmConfig
from ray.rllib.core import COMPONENT_RL_MODULE
from ray.rllib.core.columns import Columns
from ray.rllib.core.learner import Learner
from ray.rllib.core.rl_module.marl_module import MultiAgentRLModuleSpec
from ray.rllib.env.single_agent_episode import SingleAgentEpisode
from ray.rllib.policy.sample_batch import MultiAgentBatch, SampleBatch
from ray.rllib.utils.compression import unpack_if_needed
from ray.rllib.utils.typing import EpisodeType, ModuleID

logger = logging.getLogger(__name__)

# TODO (simon): Implement schema mapping for users, i.e. user define
# which row name to map to which default schema name below.
SCHEMA = [
    Columns.EPS_ID,
    Columns.AGENT_ID,
    Columns.MODULE_ID,
    Columns.OBS,
    Columns.ACTIONS,
    Columns.REWARDS,
    Columns.INFOS,
    Columns.NEXT_OBS,
    Columns.TERMINATEDS,
    Columns.TRUNCATEDS,
    Columns.T,
    # TODO (simon): Add remove as soon as we are new stack only.
    "agent_index",
    "dones",
    "unroll_id",
]


class OfflineData:
    def __init__(self, config: AlgorithmConfig):

        self.config = config
        self.is_multi_agent = config.is_multi_agent()
        self.path = (
            config.get("input_")
            if isinstance(config.get("input_"), list)
            else Path(config.get("input_"))
        )
        # Use `read_json` as default data read method.
        self.data_read_method = config.input_read_method
        # Override default arguments for the data read method.
        self.data_read_method_kwargs = (
            self.default_read_method_kwargs | config.input_read_method_kwargs
        )
        try:
            # Load the dataset.
            self.data = getattr(ray.data, self.data_read_method)(
                self.path, **self.data_read_method_kwargs
            )
            logger.info("Reading data from {}".format(self.path))
            logger.info(self.data.schema())
        except Exception as e:
            logger.error(e)
        # Avoids reinstantiating the batch iterator each time we sample.
        self.batch_iterator = None
        # For remote learner setups.
        self.locality_hints = None
        self.learner_handles = None
        self.module_spec = None

    def sample(
        self,
        num_samples: int,
        return_iterator: bool = False,
        num_shards: int = 1,
    ):
        if (
            not return_iterator
            or return_iterator
            and num_shards <= 1
            and not self.batch_iterator
        ):
            # If no iterator should be returned, or if we want to return a single
            # batch iterator, we instantiate the batch iterator once, here.
            # TODO (simon, sven): The iterator depends on the `num_samples`, i.e.abs
            # sampling later with a different batch size would need a
            # reinstantiation of the iterator.
            self.batch_iterator = self.data.map_batches(
                OfflinePreLearner,
                fn_constructor_kwargs={
                    "config": self.config,
                    "learner": self.learner_handles[0],
                },
                concurrency=2,
                batch_size=num_samples,
            ).iter_batches(
                batch_size=num_samples,
                prefetch_batches=2,
                local_shuffle_buffer_size=num_samples * 10,
            )

        # Do we want to return an iterator or a single batch?
        if return_iterator:
            # In case of multiple shards, we return multiple
            # `StreamingSplitIterator` instances.
            if num_shards > 1:
                # Call here the learner to get an up-to-date module state.
                # TODO (simon): This is a workaround as along as learners cannot
                # receive any calls from another actor.
                module_state = ray.get(
                    self.learner_handles[0].get_state.remote(
                        component=COMPONENT_RL_MODULE
                    )
                )
                return self.data.map_batches(
                    # TODO (cheng su): At best the learner handle passed in here should
                    # be the one from the learner that is nearest, but here we cannot
                    # provide locality hints.
                    OfflinePreLearner,
                    fn_constructor_kwargs={
                        "config": self.config,
                        "learner": self.learner_handles,
                        "locality_hints": self.locality_hints,
                        "module_spec": self.module_spec,
                        "module_state": module_state,
                    },
                    concurrency=num_shards,
                    batch_size=num_samples,
                    zero_copy_batch=True,
                ).streaming_split(
                    n=num_shards, equal=False, locality_hints=self.locality_hints
                )

            # Otherwise, we return a simple batch `DataIterator`.
            else:
                return self.batch_iterator
        else:
            # Return a single batch from the iterator.
            return next(iter(self.batch_iterator))["batch"][0]

    @property
    def default_read_method_kwargs(self):
        return {
            "override_num_blocks": max(self.config.num_learners * 2, 2),
        }


class OfflinePreLearner:
    def __init__(
        self,
        config,
        learner: Union[Learner, list[ActorHandle]],
        locality_hints: Optional[list] = None,
        module_spec: Optional[MultiAgentRLModuleSpec] = None,
        module_state: Optional[Dict[ModuleID, Any]] = None,
    ):

        self.config = config
        # We need this learner to run the learner connector pipeline.
        # If it is a `Learner` instance, the `Learner` is local.
        if isinstance(learner, Learner):
            self._learner = learner
            self.learner_is_remote = False
            self._module = self._learner._module
        # Otherwise we have remote `Learner`s.
        else:
            # TODO (simon): Check with the data team how to get at
            # initialization the data block location.
            node_id = ray.get_runtime_context().get_node_id()
            # Shuffle indices such that not each data block syncs weights
            # with the same learner in case there are multiple learners
            # on the same node like the `PreLearner`.
            indices = list(range(len(locality_hints)))
            random.shuffle(indices)
            locality_hints = [locality_hints[i] for i in indices]
            learner = [learner[i] for i in indices]
            # Choose a learner from the same node.
            for i, hint in enumerate(locality_hints):
                if hint == node_id:
                    self._learner = learner[i]
            # If no learner has been chosen, there is none on the same node.
            if not self._learner:
                # Then choose a learner randomly.
                self._learner = learner[random.randint(0, len(learner) - 1)]
            self.learner_is_remote = True
            # Build the module from spec. Note, this will be a MARL module.
            self._module = module_spec.build()
            self._module.set_state(module_state)
        # Build the learner connector pipeline.
        self._learner_connector = self.config.build_learner_connector(
            input_observation_space=None,
            input_action_space=None,
        )
        # Cache the policies to be trained to update weights only for these.
        self._policies_to_train = self.config.policies_to_train
        self._is_multi_agent = config.is_multi_agent()
        # Set the counter to zero.
        self.iter_since_last_module_update = 0
        # self._future = None

    def __call__(self, batch: Dict[str, np.ndarray]) -> Dict[str, List[EpisodeType]]:
        # Map the batch to episodes.
        episodes = self._map_to_episodes(self._is_multi_agent, batch)
        # TODO (simon): Make synching work. Right now this becomes blocking or never
        # receives weights. Learners appear to be non accessable via other actors.
        # Increase the counter for updating the module.
        # IDEA: put the module state into the object store. From there any actor has
        # access.
        # self.iter_since_last_module_update += 1

        # if self._future:
        #     refs, _ = ray.wait([self._future], timeout=0)
        #     print(f"refs: {refs}")
        #     if refs:
        #         module_state = ray.get(self._future)
        #
        #         self._module.set_state(module_state)
        #         self._future = None

        # # Synch the learner module, if necessary. Note, in case of a local learner
        # # we have a reference to the module and therefore an up-to-date module.
        # if self.learner_is_remote and self.iter_since_last_module_update
        # > self.config.prelearner_module_synch_period:
        #     # Reset the iteration counter.
        #     self.iter_since_last_module_update = 0
        #     # Request the module weights from the remote learner.
        #     self._future =
        # self._learner.get_module_state.remote(inference_only=False)
        #     # module_state =
        # ray.get(self._learner.get_module_state.remote(inference_only=False))
        #     # self._module.set_state(module_state)

        # Run the `Learner`'s connector pipeline.
        batch = self._learner_connector(
            rl_module=self._module,
            data={},
            episodes=episodes["episodes"],
            shared_data={},
        )
        # Convert to `MultiAgentBatch`.
        batch = MultiAgentBatch(
            {
                module_id: SampleBatch(module_data)
                for module_id, module_data in batch.items()
            },
            # TODO (simon): This can be run once for the batch and the
            # metrics, but we run it twice: here and later in the learner.
            env_steps=sum(e.env_steps() for e in episodes["episodes"]),
        )
        # Remove all data from modules that should not be trained. We do
        # not want to pass around more data than necessaty.
        for module_id in list(batch.policy_batches.keys()):
            if not self._should_module_be_updated(module_id, batch):
                del batch.policy_batches[module_id]

        # TODO (simon): Log steps trained for metrics (how?). At best in learner
        # and not here. But we could precompute metrics here and pass it to the learner
        # for logging. Like this we do not have to pass around episode lists.

        # TODO (simon): episodes are only needed for logging here.
        return {"batch": [batch]}

    def _should_module_be_updated(self, module_id, multi_agent_batch=None):
        """Checks which modules in a MARL module should be updated."""
        if not self._policies_to_train:
            # In case of no update information, the module is updated.
            return True
        elif not callable(self._policies_to_train):
            return module_id in set(self._policies_to_train)
        else:
            return self._policies_to_train(module_id, multi_agent_batch)

    @staticmethod
    def _map_to_episodes(
        is_multi_agent: bool, batch: Dict[str, np.ndarray]
    ) -> Dict[str, List[EpisodeType]]:
        """Maps a batch of data to episodes."""

        episodes = []
        # TODO (simon): Give users possibility to provide a custom schema.
        for i, obs in enumerate(batch["obs"]):

            # If multi-agent we need to extract the agent ID.
            # TODO (simon): Check, what happens with the module ID.
            if is_multi_agent:
                agent_id = (
                    batch[Columns.AGENT_ID][i]
                    if Columns.AGENT_ID in batch
                    # The old stack uses "agent_index" instead of "agent_id".
                    # TODO (simon): Remove this as soon as we are new stack only.
                    else (batch["agent_index"][i] if "agent_index" in batch else None)
                )
            else:
                agent_id = None

            if is_multi_agent:
                # TODO (simon): Add support for multi-agent episodes.
                pass
            else:
                # Build a single-agent episode with a single row of the batch.
                episode = SingleAgentEpisode(
                    id_=batch[Columns.EPS_ID][i],
                    agent_id=agent_id,
                    observations=[
                        unpack_if_needed(obs),
                        unpack_if_needed(batch[Columns.NEXT_OBS][i]),
                    ],
                    infos=[
                        {},
                        batch[Columns.INFOS][i] if Columns.INFOS in batch else {},
                    ],
                    actions=[batch[Columns.ACTIONS][i]],
                    rewards=[batch[Columns.REWARDS][i]],
                    terminated=batch[
                        Columns.TERMINATEDS if Columns.TERMINATEDS in batch else "dones"
                    ][i],
                    truncated=batch[Columns.TRUNCATEDS][i]
                    if Columns.TRUNCATEDS in batch
                    else False,
                    # TODO (simon): Results in zero-length episodes in connector.
                    # t_started=batch[Columns.T if Columns.T in batch else
                    # "unroll_id"][i][0],
                    # TODO (simon): Single-dimensional columns are not supported.
                    extra_model_outputs={
                        k: [v[i]] for k, v in batch.items() if k not in SCHEMA
                    },
                    len_lookback_buffer=0,
                )
            episodes.append(episode)
        # Note, `map_batches` expects a `Dict` as return value.
        return {"episodes": episodes}
