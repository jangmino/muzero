import typing
import unittest
import os

import numpy as np

from Games.hex.HexGame import HexGame
from Games.hex.MuZeroModel.NNet import NNetWrapper as HexNet

from MuZero.MuMCTS import MuZeroMCTS

from utils import DotDict
from utils.loss_utils import scalar_to_support, support_to_scalar, atari_reward_transform, \
    inverse_atari_reward_transform
from utils.selfplay_utils import GameHistory


class TestStaticFunctions(unittest.TestCase):

    def test_reward_scale_transformation(self):
        var_eps = 0.01  # Paper uses 0.001, but correct function implementation is easier tested with 0.01
        positive_rewards = np.arange(0, 101)
        negative_rewards = np.arange(-100, 1)

        f_positive = atari_reward_transform(positive_rewards, var_eps=var_eps)
        f_negative = atari_reward_transform(negative_rewards, var_eps=var_eps)

        # Assert symmetry
        np.testing.assert_array_almost_equal(f_positive, -f_negative[::-1])

        # Assert correct mapping for var_eps = 0.001. -> f(100) \equiv sqrt(101)
        self.assertAlmostEqual(var_eps, 0.01)
        self.assertAlmostEqual(f_positive[-1], np.sqrt(positive_rewards[-1] + 1))
        self.assertAlmostEqual(f_positive[0], 0)

        inv_positive = inverse_atari_reward_transform(f_positive, var_eps=var_eps)
        inv_negative = inverse_atari_reward_transform(f_negative, var_eps=var_eps)

        # Assert inverse working correctly.
        np.testing.assert_array_almost_equal(inv_positive, positive_rewards)
        np.testing.assert_array_almost_equal(inv_negative, negative_rewards)

    def test_reward_distribution_transformation(self):
        bins = 300  # Ensure that bins is large enough to support 'high'.
        n = 10      # Number of samples to draw
        high = 1e3  # Factor to scale the randomly generated rewards

        # Generate some random (large) values
        scalars = np.random.randn(n) * high

        # Cast scalars to support points of a categorical distribution.
        support = scalar_to_support(scalars, bins)

        # Ensure correct dimensionality
        self.assertEqual(support.shape, (n, bins * 2 + 1))

        # Cast support points back to scalars.
        inverted = support_to_scalar(support, bins)

        # Ensure correct dimensionality
        self.assertEqual(inverted.shape, scalars.shape)

        # Scalar to support and back to scalars should be equal.
        np.testing.assert_array_almost_equal(scalars, inverted)

    def test_n_step_return_estimation_MDP(self):
        horizon = 3    # n-step lookahead for computing z_t
        gamma = 1 / 2  # discount factor for future rewards and bootstrap

        # Experiment settings
        search_results = [5, 5, 5, 5, 5]  # MCTS v_t index +k
        dummy_rewards = [0, 1, 2, 3, 4]   # u_t+1 index +k
        z = 0                             # Final return provided by the env.

        # Desired output: Correct z_t index +k (calculated manually)
        n_step = [1 + 5/8, 3 + 3/8, 4 + 1/2, 5.0, 4.0, 0]

        # Fill the GameHistory with the required data.
        h = GameHistory()
        for r, v in zip(dummy_rewards, search_results):
            h.capture(np.array([]), -1, 1, np.array([]), r, v)
        h.terminate(np.array([]), 1, z)

        # Check if algorithm computes z_t's correctly
        h.compute_returns(gamma, horizon)
        np.testing.assert_array_almost_equal(h.observed_returns[:-1], n_step[:-1])

    def test_observation_stacking(self):
        # random normal variables in the form (x, y, c)
        shape = (3, 3, 8)
        dummy_observations = [np.random.randn(np.prod(shape)).reshape(shape) for _ in range(10)]

        h = GameHistory()
        h.capture(dummy_observations[0], -1, 1, np.array([]), 0, 0)

        # Ensure correct shapes and content
        stacked_0 = h.stackObservations(0)
        stacked_1 = h.stackObservations(1)
        stacked_5 = h.stackObservations(5)

        np.testing.assert_array_equal(stacked_0.shape, shape)  # Shape didn't change
        np.testing.assert_array_equal(stacked_1.shape, shape)  # Shape didn't change
        np.testing.assert_array_equal(stacked_5.shape, np.array(shape) * np.array([1, 1, 5]))  # Channels * 5

        np.testing.assert_array_almost_equal(stacked_0, dummy_observations[0])  # Should be the same
        np.testing.assert_array_almost_equal(stacked_1, dummy_observations[0])  # Should be the same

        np.testing.assert_array_almost_equal(stacked_5[..., :-8], 0)            # Should be only 0s
        np.testing.assert_array_almost_equal(stacked_5[..., -8:], dummy_observations[0])  # Should be the first o_t

        # Check whether observation concatenation works correctly
        stacked = h.stackObservations(2, dummy_observations[1])
        expected = np.concatenate(dummy_observations[:2], axis=-1)
        np.testing.assert_array_almost_equal(stacked, expected)

        # Fill the buffer
        all([h.capture(x, -1, 1, np.array([]), 0, 0) for x in dummy_observations[1:]])

        # Check whether time indexing works correctly
        stacked_1to5 = h.stackObservations(4, t=4)   # 1-4 --> t is inclusive
        stacked_last4 = h.stackObservations(4, t=9)  # 6-9
        expected_1to5 = np.concatenate(dummy_observations[1:5], axis=-1)   # t in {1, 2, 3, 4}
        expected_last4 = np.concatenate(dummy_observations[-4:], axis=-1)  # t in {6, 7, 8, 9}

        np.testing.assert_array_almost_equal(stacked_1to5, expected_1to5)
        np.testing.assert_array_almost_equal(stacked_last4, expected_last4)

        # Check if clearing works correctly
        h.refresh()
        self.assertEqual(len(h), 0)


class TestHexMuZero(unittest.TestCase):
    """
    Unit testing class to test whether the search engine exhibit well defined behaviour.
    This includes scenarios where either the model or inputs are faulty (empty observations,
    constant predictions, nans/ inf in observations).
    """
    hex_board_size: int = 5

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Setup required for unit tests.
        print("Unit testing CWD:", os.getcwd())
        self.config = DotDict.from_json("../Experimenter/MuZeroConfigs/default.json")
        self.g = HexGame(self.hex_board_size)
        self.net = HexNet(self.g, self.config.net_args)
        self.mcts = MuZeroMCTS(self.g, self.net, self.config.args)

    def test_empty_input(self):
        """
        Tests the following scenarios:
         - Assert that observation tensors with only zeros are encoded to finite values (can be zero)
         - Assert that latent state tensors with only zeros are transitioned to finite values (can be zero)
        """
        # Build the environment for an observation.
        s = self.g.getInitialState()
        o_t = self.g.buildObservation(s, player=1, form=self.g.Representation.HEURISTIC)
        h = GameHistory()

        # Build empty observations
        h.capture(o_t, -1, 1, np.array([]), 0, 0)
        stacked = h.stackObservations(self.net.net_args.observation_length, o_t)
        zeros_like = np.zeros_like(stacked)

        # Check if nans are produced
        latent = self.net.encode(zeros_like)
        self.assertTrue(np.isfinite(latent).all())

        # Exhaustively ensure that all possible dynamics function inputs lead to finite values.
        latent_forwards = [self.net.forward(latent, action)[1] for action in range(self.g.getActionSize())]
        self.assertTrue(np.isfinite(np.array(latent_forwards)).all())

    def test_search_recursion_error(self):
        """
        The main phenomenon this test attempts to find is:
        Let s be the current latent state, s = [0, 0, 0], along with action a = 1.
        If we fetch the next latent state with (s, a) we do not want to get, s' == s = [0, 0, 0].
        s' is a new state, although it is present in the transition table due to being identical to s.
        if action a = 1 is chosen again by UCB, then this could result in infinite recursion.

        Tests the following scenarios:
         - Assert that MuMCTS does not result in a recursion error when called with the same
           input multiple times without clearing the tree.
         - Assert that MuMCTS does not result in a recursion error when inputs are either zero
           or random.
         - Assert that MuMCTS does not result in a recursion error when only one root action is legal.
        """
        rep = 30  # Repetition factor --> should be high.

        # Build the environment for an observation.
        s = self.g.getInitialState()
        o_t = self.g.buildObservation(s, player=1, form=self.g.Representation.HEURISTIC)
        h = GameHistory()

        # Build empty and random observations tensors
        h.capture(o_t, -1, 1, np.array([]), 0, 0)
        stacked = h.stackObservations(self.net.net_args.observation_length, o_t)
        zeros_like = np.zeros_like(stacked)
        random_like = np.random.rand(*zeros_like.shape)

        # Build root state legal action masks
        legals = np.ones(self.g.getActionSize())
        same = np.zeros_like(legals)
        same[0] = 1  # Can only do one move

        # Execute multiple MCTS runs that will result in recurring tree paths.
        for _ in range(rep):
            self.mcts.runMCTS(zeros_like, legals)  # Empty observations ALL moves at the root
        self.mcts.clear_tree()

        for _ in range(rep):
            self.mcts.runMCTS(zeros_like, same)    # Empty observations ONE move at the root
        self.mcts.clear_tree()

        for _ in range(rep):
            self.mcts.runMCTS(random_like, legals)  # Empty observations ALL moves at the root
        self.mcts.clear_tree()

        for _ in range(rep):
            self.mcts.runMCTS(random_like, same)  # Empty observations ONE move at the root
        self.mcts.clear_tree()

    def test_search_border_cases_latent_state(self):
        """
        Tests the following scenarios:
        - Assert that observation tensors with only infinities or nans result in finite tensors (zeros).
          Testing this phenomenon ensures that bad input is not propagated for more than one step.
          Note that one forward step using bad inputs can already lead to a recursion error in MuMCTS.
          see test_search_recursion_error
       """
        # Build the environment for an observation.
        s = self.g.getInitialState()
        o_t = self.g.buildObservation(s, player=1, form=self.g.Representation.HEURISTIC)
        h = GameHistory()

        # Build empty observations
        h.capture(o_t, -1, 1, np.array([]), 0, 0)
        stacked = h.stackObservations(self.net.net_args.observation_length, o_t)
        nans_like = np.zeros_like(stacked)
        inf_like = np.zeros_like(stacked)

        nans_like[nans_like == 0] = np.nan
        inf_like[inf_like == 0] = np.inf

        # Check if nans are produced
        nan_latent = self.net.encode(nans_like)
        inf_latent = self.net.encode(inf_like)

        self.assertTrue(np.isfinite(nan_latent).all())
        self.assertTrue(np.isfinite(inf_latent).all())

        nan_latent[nan_latent == 0] = np.nan
        inf_latent[inf_latent == 0] = np.inf

        # Exhaustively ensure that all possible dynamics function inputs lead to finite values.
        nan_latent_forwards = [self.net.forward(nan_latent, action)[1] for action in range(self.g.getActionSize())]
        inf_latent_forwards = [self.net.forward(inf_latent, action)[1] for action in range(self.g.getActionSize())]

        self.assertTrue(np.isfinite(np.array(nan_latent_forwards)).all())
        self.assertTrue(np.isfinite(np.array(inf_latent_forwards)).all())

    def test_ill_conditioned_model(self):
        """
        Execute all unit tests of this class using a model with badly conditioned weights.
        i.e., large weight magnitudes or only zeros.
        """
        class DumbModel(HexNet):

            def forward(self, latent_state: np.ndarray, action: int) -> typing.Tuple[float, np.ndarray]:
                return 0, np.zeros_like(latent_state)

            def predict(self, latent_state: np.ndarray) -> typing.Tuple[np.ndarray, float]:
                pi, v = super().predict(latent_state)
                return np.random.uniform(size=len(pi)), 0

        memory_net = self.net
        memory_search = self.mcts

        # Swap class variables
        self.net = DumbModel(self.g, self.config.net_args)
        self.mcts = MuZeroMCTS(self.g, self.net, self.config.args)

        self.test_search_recursion_error()

        # Undo class variables swap
        self.net = memory_net
        self.mcts = memory_search


class TestSelfPlay(unittest.TestCase):

    def test_history_building(self):
        pass


if __name__ == '__main__':
    unittest.main()
