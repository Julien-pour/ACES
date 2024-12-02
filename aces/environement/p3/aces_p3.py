import random
from typing import List, Dict
from aces.llm_client import LLMClient
from dataclasses import dataclass, field
import json
from aces.environement.p3.p3_genotype import P3
from aces.environement.p3.prompt_function import get_prompt_label_p3, get_prompt_description_p3, prompt_solve_puzzle_given_f, get_programming_puzzles_prompt
from aces.environement.p3.skill_list import skill_list
from aces.environement.p3.utils import extract_skill, extract_solution, extract_f
from aces.code_sandbox import evaluate, pass_at_k
import numpy as np
import os
from itertools import combinations
from scipy.spatial.distance import cdist


os.environ["TOKENIZERS_PARALLELISM"] = "true"
#TODO inherite from base ACES class with common stuff
class ACES_p3:
    def __init__(self, AcesArguments: dataclass, LLMArguments : dataclass):
        # initialize LLM client
        self.llm_args = LLMArguments
        self.skill_list = skill_list
        self.id = 0
        self.init_llm()
        # initialize environment
        self.aces_args = AcesArguments 
        self.initialize_environment()
        self.archive = []
        
        self.niche_to_idx_archive = {}
        self.semantic_descriptors = []
        self.fitnesses = []
        self.rng = np.random.default_rng(self.aces_args.seed)


    def niches_filled(self):
        """Get the number of niches that have been explored in the map."""
        return len(self.niche_to_idx_archive.keys())

    def max_fitness(self):
        """Get the maximum fitness value in the map."""
        return (np.array(self.fitnesses)[np.isfinite(self.fitnesses)]).max()

    def mean_fitness(self):
        """Get the mean fitness value in the map."""
        return (np.array(self.fitnesses)[np.isfinite(self.fitnesses)]).mean()

    def min_fitness(self):
        """Get the minimum fitness value in the map."""
        return (np.array(self.fitnesses)[np.isfinite(self.fitnesses)]).min()

    def init_llm(self,) -> None:
        """init LLM client"""
        print("init LLM client")
        cfg_generation ={"model": self.llm_args.model_name_or_path, "temperature": self.llm_args.temperature,  "max_tokens": self.llm_args.max_tokens}

        self.llm = LLMClient(model = self.llm_args.model_name_or_path, 
                             cfg_generation = cfg_generation,
                             base_url = self.llm_args.base_url, 
                             api_key = self.llm_args.api_key, 
                             online = self.llm_args.online, 
                             gpu = self.llm_args.gpu,
                             max_model_length = self.llm_args.max_model_length)
        print("LLM client initialized")
    
    def initialize_environment(self) -> None:
        with open(self.aces_args.path_archive, 'r') as f:
            self.archives = json.load(f)
        list_p3 = []

        # generate semantic descriptor
        for p in self.archives:
            list_p3.append(P3(program_str = p['program_str']))
        list_p3 = self.generate_semantic_descriptors(list_p3)
        
        # generate dfficulty
        ## generate multiple solutions
        list_p3 = self.generate_multiple_solutions(list_p3)
        ## evaluate python code
        list_p3 = self.evaluate_python_code(list_p3)
        ## generate description
        list_p3 = self.generate_description(list_p3)
        # rm_fitness_condition = True because initial puzzles should be solvable
        self.update_archive(list_p3, rm_fitness_condition = True)

    
    def update_archive(self,list_p3: list[P3], rm_fitness_condition = False):
        """update archive with valid puzzles"""
        for p in list_p3:
            condition_add_individual = p.fitness != -np.inf
            if rm_fitness_condition:
                condition_add_individual = True
            if condition_add_individual:
                niche_idx = tuple(p.emb)
                p.id = self.id
                self.archives.append(p)
                self.fitnesses.append(p.fitness)
                if not niche_idx in self.niche_to_idx_archive:
                    self.niche_to_idx_archive[niche_idx] = []
                self.niche_to_idx_archive[niche_idx].append(self.id)
                self.id +=1
                


    def formating_chat_prompt(self, list_prompt_str: list[str]) -> list[list[dict]]:
        """Format list of prompt string to chat prompt"""
        list_prompt_chat=[]
        for prompt in list_prompt_str:
            # check whether I used syst prompt or not
            list_prompt_chat.append([{"role": "user", "content": prompt}])
        return list_prompt_chat
    
    def generate_multiple_solutions(self, puzzles: list[P3]) -> List[P3]:
        """Use LLM to generate multiple solutions for a list of puzzle"""
        list_prompt_sol = []
        for p in puzzles:
            list_prompt_sol.append(prompt_solve_puzzle_given_f(p.program_str))
        list_solutions = self.llm.multiple_completion(self.formating_chat_prompt(list_prompt_sol),n = self.aces_args.num_solutions)
        assert len(list_solutions) == len(puzzles)
        for id_puzzle in range(len(puzzles)):
            problem = puzzles[id_puzzle].program_str 
            n_solutions = [self.process_solutions(solution=sol,problem=problem) for sol in list_solutions[id_puzzle].response]
            puzzles[id_puzzle].all_solution = n_solutions
        # don't forget to verify solution with python
        return puzzles
    

    def process_solutions(self, solution: str, problem: str) -> str: 
        """Process solution and return full puzzle (f+g)"""
        puzzle = extract_f(problem) + "\n" + extract_solution(solution)
        puzzle = puzzle.split("\nassert f")
        puzzle = puzzle[0] + "\nassert f(g()) == True\n"
        return puzzle
    
    def evaluate_python_code(self, puzzles: list[P3]) -> List[P3]:
        """Evaluate python code"""
        list_task_id = []
        list_task_id_unique = []
        list_codes_to_test = []
        str_to_add=str(
                    f"\ndef run_eval():\n"
                    f"    return f(g()) == True"
                )
        for id_puz,p in enumerate(puzzles):
            list_task_id_unique.append(id_puz)
            for id_sol in range(len(p.all_solution)):
                list_task_id.append(id_puz)
                list_codes_to_test.append(p.all_solution[id_sol] + str_to_add)


        results = evaluate(list_codes_to_test, list_task_id, entry_point="run_eval")
        # dic_passk = results["pass@k"] # {task_id: pass@k} 
        raw_result = results["raw_result"] 
        for task_id in list_task_id_unique:
            all_solution = []
            all_solution_correct = []
            for id_completion in range(len(raw_result[task_id])):
                all_solution.append(raw_result[task_id][id_completion]["code"].split(str_to_add)[0])
                all_solution_correct.append(raw_result[task_id][id_completion]["correct"])
            
            puzzles[task_id].all_solution = all_solution
            puzzles[task_id].all_solution_correct = all_solution_correct

            number_solution = len(all_solution)
            c = sum(all_solution_correct)
            k=1 # estimation of pass@1
            
            if c==0:
                fitness = -np.inf
            else:
                fitness = pass_at_k(n=number_solution, c=c, k=k)
                list_correct_solution = [all_solution[i] for i in range(len(all_solution)) if all_solution_correct[i]]
                id_rd = random.randint(0,len(list_correct_solution)-1)
                puzzles[task_id].program_str = list_correct_solution[id_rd]
            puzzles[task_id].fitness = fitness

        return puzzles
    

    def generate_semantic_descriptors(self, puzzles: list[P3]) -> list[P3]:
        # Use LLM to evaluate puzzle along N programming skill dimensions
        # get prompt
        list_prompt = []
        for p in puzzles:
            list_prompt.append(get_prompt_label_p3(p.program_str, self.skill_list))
        list_prompt_chat = self.formating_chat_prompt(list_prompt)
        list_skills = self.llm.multiple_completion(list_prompt_chat)
        assert len(list_skills) == len(puzzles)
        for i in range(len(puzzles)):
            skill, explanation_skill = extract_skill(list_skills[i].response[0],skill=len(self.skill_list))
            puzzles[i].emb = skill
            puzzles[i].explanation_emb = explanation_skill
            # puzzle[i].phenotype = skill
        return puzzles
    
    def generate_description(self, puzzles: list[P3]) -> list[P3]:
        # Use LLM to evaluate puzzle along N programming skill dimensions
        # get prompt
        list_prompt = []
        for p in puzzles:
            list_prompt.append(get_prompt_description_p3(p.program_str))
        list_description = self.llm.multiple_completion(self.formating_chat_prompt(list_prompt))
        for i in range(len(puzzles)):
            puzzles[i].description = list_description[i].response[0]
        return puzzles

    def generate_new_puzzles(self,list_goal_with_examples):
        list_prompt = []
        difficulty_range = (self.aces_args.difficulty_min_target,self.aces_args.difficulty_max_target)
        list_few_shot_ex_id = []
        list_goal = []
        for (list_few_shot_example_phenotypes, goal) in list_goal_with_examples:
            list_few_shot_ex_id.append([ex["id"] for ex in list_few_shot_example_phenotypes])
            list_goal.append(goal)
            prompt = get_programming_puzzles_prompt(list_few_shot_example_phenotypes,goal,
                        puzzle_generation_strategy = self.aces_args.puzzle_generation_strategy,
                        puzzle_generation_strategy=difficulty_range)
            
            list_prompt.append(prompt)

        list_prompt_chat = self.formating_chat_prompt(list_prompt)
        news_puzzles = self.llm.multiple_completion(list_prompt_chat)
        #TODO: exctract puzzles + ...
        list_new_p3 = []
        
        for id_puzzle,puzzle in enumerate(news_puzzles):
            split_puzzles = puzzle.replace("```python","```").replace("``` python","```").split("```")
            for idx in range(len(split_puzzles)):
                if "def f" in split_puzzles[idx] and "def g" in split_puzzles[idx]:
                    split_puzzles[idx] = split_puzzles[idx].split("\nassert f(")[0]
                    split_puzzles[idx] = split_puzzles[idx] + "\nassert f(g()) == True\n"
                    new_p3 = P3(split_puzzles[idx],target_skills=list_goal[id_puzzle],puzzles_id_fewshot=list_few_shot_ex_id[id_puzzle])
                    list_new_p3.append(new_p3)
                    
        return list_new_p3

    def sample_goals(self,):
        """
        Sample goals in the semantic space (combination of skills)
        out: list[goal] with goal: list[0/1] and size(goal) = len(self.skill_list)
        """
        n_goals_to_sample = self.aces_args.batch_size
        n_skills = len(self.skill_list)
        list_skill_targeted = []
        skills = list(range(1, n_skills+1))
        # Generate all combinations of up to 5 skills
        skill_combinations = set()
        for r in range(1, self.aces_args.max_descriptor_targeted+1):  # From 1 skill to 5 skills
            skill_combinations.update(combinations(skills, r))
        skill_combinations = list(skill_combinations)
        match self.aces_args.mode_sampling_goal:
            case 'uniform':
                list_idx = self.rng.choice(len(skill_combinations),size=n_goals_to_sample,replace=True)
                for idx in list_idx:
                    out = skill_combinations[idx]
                    skill_targeted = [1 if i in out else 0 for i in range(n_skills)]
                    list_skill_targeted.append(skill_targeted)
            case 'smart':
                # TODO: verify smart is working
                all_emb = list(self.niche_to_idx_archive.keys())
                all_emb = np.array([list(i) for i in all_emb]) # list of all explored niches
                
                skill_combinations_bin = [[1 if i in vec else 0 for i in range(n_skills)] for vec in skill_combinations] #list of all possible niches 
                
                #compute distance between all possible niche and all explored niches
                out=cdist(skill_combinations_bin, all_emb, metric='cityblock') 
                density=(out==1).sum(axis=1) # find every niches within a distance of 1
                density=density*(out.min(axis=1)!=0) # remove already explored niches (sampling weight = 0)
                norm= np.sum(density)
                if norm == 0.:
                    norm=1
                density_norm=density/norm

                list_idx_niches_sampled=np.random.choice(len(skill_combinations_bin),p=density_norm,size=n_goals_to_sample)
                for idx_niches_sampled in list_idx_niches_sampled:
                    binary_vectors_sampled=skill_combinations_bin[idx_niches_sampled]
                    target_skill=list(binary_vectors_sampled)
                    target_skill = [int(element) for element in target_skill]
                    list_skill_targeted.append(target_skill)
                return list_skill_targeted
            case 'none':
                list_skill_targeted = []
        return list_skill_targeted

    def sample_goal_with_examples(self):
        """sample goal and examples in context
        out: list[(list[P3],list[goal]) 
        with goal: list[0/1] and size(goal) = len(self.skill_list)
        list[P3] example to use in context, they are selected among the closest niches,
        and they each example sample from a different niche 
        """
        list_goal_with_examples =[]
        list_goal = self.sample_goals()
        for goal in list_goal:
            list_archive_index = []
            
            all_emb = list(self.niche_to_idx_archive.keys())
            all_emb = np.array([list(i) for i in all_emb])

            list_coord_niches_sampled = []
            
            # compute distance between all cells explored and the target cell
            dists = cdist([goal], all_emb)[0]

            # shuffle indices to have true uniform sampling of closest niches
            # (otherwise, if two niches are at the same distance, the first one will be always sampled)
            shuffled_indices = np.arange(len(dists))
            np.random.shuffle(shuffled_indices)
            nearest_niches = shuffled_indices[np.argsort(dists[shuffled_indices])]
            
            for idx in nearest_niches:
                niche_idx = list(self.niche_to_idx_archive.keys())[idx]
                if not(niche_idx in list_coord_niches_sampled):
                    list_coord_niches_sampled.append(niche_idx)
                    archive_indexs = self.sample_examples_from_niche(niche_idx)
                    list_archive_index.append(archive_indexs)
                if len(list_archive_index)>=self.aces_args.n_fewshot_examples:
                    break
            list_few_shot_example_phenotypes = [self.archive[idx] for idx in list_archive_index]
            list_goal_with_examples.append((list_few_shot_example_phenotypes, goal))
        return list_goal_with_examples

    def sample_examples_from_niche(self,niche_idx) -> int:
        """Sample one example from a niche"""

        size_niche = len(self.niche_to_idx_archive[niche_idx])
        if size_niche == 0:
            raise ValueError('Empty niche')
        if size_niche == 1:
            archive_index = self.rng.choice(self.niche_to_idx_archive[niche_idx])
            archive_index = int(archive_index)
            return archive_index
        match self.aces_args.sampling_strategy_examples_from_niche:
            case 'uniform':
                # sample a random niche
                # print(f'nonzero {self.nonzero}')
                
                archive_index = self.rng.choice(self.niche_to_idx_archive[niche_idx]) # sample a random individual
            case 'prob_best_5':
                # self.nonzero[niche_idx]
                # sort_keys = sorted(lisself.nonzero.keys())
                fitness_range = [self.min_fitness(), self.max_fitness()]  # can these be -inf/+inf?
                # sort indices by fitness
                fit_idx = [(idx, self.fitnesses[idx]) for idx in self.niche_to_idx_archive[niche_idx]]
                print(f'fitnesses {[f for _, f in fit_idx]}')
                print(f'fitness range {fitness_range}')
                fit_idx = sorted(fit_idx, key=lambda x: x[1])[::-1][:5]  # 5 most fit
                if fitness_range[1] - fitness_range[0] == 0:
                    L = 1.
                else:
                    L = fitness_range[1] - fitness_range[0]
                normalized_fitnesses = [(f - fitness_range[0]) / L for _, f in fit_idx]
                normalized_fitnesses = np.array(normalized_fitnesses)
                if normalized_fitnesses.sum() == 0:  # all the individuals have the lowest possible fitness
                    normalized_fitnesses = np.ones_like(normalized_fitnesses) / len(normalized_fitnesses)
                else:
                    normalized_fitnesses = normalized_fitnesses / normalized_fitnesses.sum()
                print(f'probabilities {normalized_fitnesses}')
                archive_index = self.rng.choice([idx for idx, f, in fit_idx], p=normalized_fitnesses)
                
            case 'soft_normalised':
                puzz_idx = [idx for idx in self.niche_to_idx_archive[niche_idx]]
                qualities = np.array([self.fitnesses[idx] for idx in self.niche_to_idx_archive[niche_idx]])
                min_quality = qualities.min()
                max_quality = qualities.max()
                if abs(max_quality-min_quality) < 1e-6:
                    probabilities = np.ones(len(qualities)) / len(qualities)
                else:
                    normalized_qualities = (qualities - min_quality) / (max_quality - min_quality)
                    # Softmax calculation
                    temperature = self.aces_args.temperature_sampling_strategy_examples_from_niche
                    scaled_logits = normalized_qualities / temperature
                    # Subtract the max for numerical stability
                    exp_logits = np.exp(scaled_logits - np.max(scaled_logits))
                    probabilities = exp_logits / np.sum(exp_logits)
                try:
                    archive_index = self.rng.choice(puzz_idx, p=probabilities)
                except:
                    print("proba",probabilities)
                    print("quality",qualities)
                    raise ValueError('Error in softmax sampling')
            case _:
                raise NotImplementedError(f'Unrecognized sampling strategy "{self.aces_args.sampling_strategy_examples_from_niche}"')
        archive_index = int(archive_index)
        return archive_index

    def run(self, num_iterations: int):
        for _ in range(num_iterations):
            # Generate novel targets in semantic space
            # with some few shot examples that are close in the semantic space 
            list_goal_with_examples = self.sample_goal_with_examples()
            list_p3 = self.generate_new_puzzles(list_goal_with_examples)
            # generate dfficulty
            ## generate multiple solutions
            list_p3 = self.generate_multiple_solutions(list_p3)
            ## evaluate python code
            list_p3 = self.evaluate_python_code(list_p3)
            ## generate description
            list_p3 = self.generate_description(list_p3)
            self.update_archive(list_p3)
            #TODO: add save archive + debug
            

if __name__ == '__main__':
    from dataclasses import dataclass, field
    from typing import Optional

    @dataclass
    class AcesArguments:
        """
        Arguments pertaining to which model/config/tokenizer we are going to fine-tune from.DataTrainingArguments
        """

        environement_name : str = field( default = "p3", metadata={"help": "environment name"})
        path_archive : str = field( default = "/home/flowers/work/aces/aces/environement/p3/preprocess_p3_emb_dedup_puzzles.json", metadata={"help": "path to the archive"})
        num_solutions: int = field( default = 2, metadata={"help": "number of solutions to generate to compute the difficulty score"})
        batch_size: int = field( default = 2, metadata={"help": "number of puzzles to create per generation"})
        n_fewshot_examples: int = field( default = 3, metadata={"help": "number of example in context" })
        max_descriptor_targeted: int = field( default = 5, metadata={"help": "number of max descriptor to target (at most `max_descriptor_targeted` semantic descriptor sample as goal)"})
        mode_sampling_goal: int = field( default = "uniform", metadata={"help": "['uniform','smart','none'], uniform sample goal uniformely, smart: sample unexplored goal close that are within 1 of distance of already explored goal in the semantic space"})
        seed: int = field(default=0)
        sampling_strategy_examples_from_niche: str = field(default='uniform', metadata={"help": "sampling strategy to sample examples from a niche, choice: 'uniform','prob_best_5','soft_normalised'; need to explain difference"})
        temperature_sampling_strategy_examples_from_niche: float = field(default= 1., metadata={"help": "temperature softmax to sample example given their fitness given a niche"})
        puzzle_generation_strategy: str = field(default= "aces", metadata={"help":"startegy to generate new puzzle, choice: ['aces','aces_elm'] todo 'wizard_coder'"})
        difficulty_min_target: int = field(default = 90, metadata={"help":"difficulty min to target"})
        difficulty_max_target: int = field(default = 100, metadata={"help":"difficulty min to target"})

    @dataclass
    class QdArguments:
        """
        Arguments pertaining to which model/config/tokenizer we are going to fine-tune from.DataTrainingArguments
        """

        a: str = field(
            default="/home/flowers/work/hf/Qwen2.5-Coder-3B-Instruct",
            metadata={"help": "Path to pretrained model or model identifier from huggingface.co/models"}
        )

    @dataclass
    class LLMArguments:
        """
        Arguments pertaining to which model/config/tokenizer we are going to fine-tune from.DataTrainingArguments
        """

        model_name_or_path: str = field(
            default="/home/flowers/work/hf/Qwen2.5-0.5B-Instruct",
            metadata={"help": "Path to pretrained model or model identifier from huggingface.co/models"}
        )
        online: Optional[bool] = field(
            default = False,
            metadata={
                "help": "use vllm server if True else use offline vllm"
            },
        )
        base_url: Optional[str] = field(
            default="http://localhost:8000",
            metadata={
                "help": "base url for vllm server"
            },
        )
        api_key: Optional[str] = field(
            default="",
            metadata={
                "help": "api key "
            },
        )
        gpu: Optional[bool] = field(
            default = 1,
            metadata={
                "help": "number of gpus to use (vllm)"
            },
        )
        cfg_generation : Optional[bool] = field(
            default = False,
            metadata={
                "help": "use cfg generation"
            },
        ),
        temperature: Optional[float] = field(
            default = 1.0,
            metadata={
                "help": "temperature"
            },
        )
        max_tokens: Optional[int] = field(
            default = 4000,
            metadata={
                "help": "max tokens"
            },
        )
        max_model_length: Optional[int] = field(
            default = 20000,
            metadata={
                "help": "max context size"
            },
        )

    # parser = HfArgumentParser((AcesArguments,QdArguments,LLMArguments))
    # model_args, data_args, training_args = parser.parse_args_into_dataclasses()#["--output_dir", "/home/flowers/work/hf/trained/"])
    aces_args, qd_args, llm_args = AcesArguments(), QdArguments(), LLMArguments()
    aces= ACES_p3(aces_args, llm_args)