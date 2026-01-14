import abc
import random
from typing import Any, Generic, List, Optional, TypeVar

T = TypeVar("T")

class LoadBalancerStrategy(abc.ABC, Generic[T]):
    """Base class for load balancing strategies."""

    @abc.abstractmethod
    def select(self, candidates: List[T]) -> T | None:
        """Select an item from the candidates."""
        pass

class RoundRobinStrategy(LoadBalancerStrategy[T]):
    """Simple Round-Robin strategy."""
    
    def __init__(self):
        self._counter = 0

    def select(self, candidates: List[T]) -> T | None:
        if not candidates:
            return None
        
        # Simple round robin
        idx = self._counter % len(candidates)
        self._counter += 1
        return candidates[idx]

class RandomStrategy(LoadBalancerStrategy[T]):
    """Random selection strategy."""

    def select(self, candidates: List[T]) -> T | None:
        if not candidates:
            return None
        return random.choice(candidates)

class PowerOfTwoChoicesStrategy(LoadBalancerStrategy[T]):
    """Power of Two Choices (P2C) strategy.
    
    Selects two random candidates and chooses the one with the lower load.
    Requires candidates to have a 'load' attribute (int).
    """

    def select(self, candidates: List[T]) -> T | None:
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
            
        # Pick two distinct random candidates
        a, b = random.sample(candidates, 2)
        
        load_a = getattr(a, "load", float("inf"))
        load_b = getattr(b, "load", float("inf"))
        
        return a if load_a <= load_b else b

class AffinityStrategy(LoadBalancerStrategy[T]):
    """Node Affinity strategy.
    
    Prefers candidates on the same node as the client.
    Falls back to a wrapped strategy if no local candidates are available.
    
    Requires candidates to have 'config.node_name' attribute.
    """
    
    def __init__(
        self, 
        current_node_name: str, 
        fallback_strategy: LoadBalancerStrategy[T] = RoundRobinStrategy()
    ):
        self.current_node_name = current_node_name
        self.fallback = fallback_strategy
        
    def select(self, candidates: List[T]) -> T | None:
        if not candidates:
            return None
            
        # Filter local candidates
        local_candidates = []
        for c in candidates:
            # Access config.node_name safely
            config = getattr(c, "config", None)
            node = getattr(config, "node_name", None)
            if node == self.current_node_name:
                local_candidates.append(c)
                
        if local_candidates:
            # Use fallback strategy to select among local candidates
            # (e.g. RoundRobin among local pods)
            return self.fallback.select(local_candidates)
            
        # No local candidates, use fallback on all candidates
        return self.fallback.select(candidates)